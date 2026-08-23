import type { Memory } from './memory.ts';

const ALPHABET =
	'abcdefghijklmnopqrstuvwxyz' + 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' + '*\n0123456789.,!?_#\'"/\\-:()';

/**
 * Default ZSCII → Unicode translation table for codes 155..223 per Z-Machine
 * Standards Document §3.8.3. v5+ games can override with a table in the header
 * extension (see `readUnicodeTable` below).
 */
export const DEFAULT_ZSCII_EXTRA =
	'äöüÄÖÜß»«ëïÿËÏáéíóúýÁÉÍÓÚÝàèìòùÀÈÌÒÙâêîôûÂÊÎÔÛåÅøØãñõÃÑÕæÆçÇþðÞÐ£œŒ¡¿';

// ZSCII 155..251 is the range where games may supply custom Unicode translations
// (§3.8.4). The default table covers 155..223 only; custom tables may extend.
const ZSCII_EXTRA_MIN = 155;
const ZSCII_EXTRA_MAX = 251;
const MAX_CUSTOM_ENTRIES = ZSCII_EXTRA_MAX - ZSCII_EXTRA_MIN + 1; // 97

// Header extension table pointer (Z-spec §11.1.36). Word 3 within the extension
// points to the custom Unicode table.
const HEADER_EXT_PTR = 0x36;
const UNICODE_TABLE_WORD = 3;

// Custom alphabet table pointer (Z-spec §11.1.35, §3.5.5). v5+ only.
const ALPHABET_TABLE_PTR = 0x34;
const ALPHABET_TABLE_BYTES = 78; // 3 alphabets × 26 chars

/**
 * Read a game-supplied Unicode translation table (v5+ header extension).
 * Returns the full 97-char table (padded from defaults) if present, or null.
 */
export function readUnicodeTable(mem: Memory): string | null {
	const extAddr = mem.getu(HEADER_EXT_PTR);
	if (extAddr === 0) return null;
	const extLen = mem.getu(extAddr);
	if (extLen < UNICODE_TABLE_WORD) return null;
	const tableAddr = mem.getu(extAddr + 2 * UNICODE_TABLE_WORD);
	if (tableAddr === 0) return null;
	const count = mem.bytes[tableAddr]!;
	let out = '';
	for (let i = 0; i < count && i < MAX_CUSTOM_ENTRIES; i++) {
		out += String.fromCharCode(mem.getu(tableAddr + 1 + i * 2));
	}
	if (out.length < DEFAULT_ZSCII_EXTRA.length) {
		out += DEFAULT_ZSCII_EXTRA.slice(out.length);
	}
	return out;
}

/**
 * Read a game-supplied alphabet table (v5+ header byte 0x34). Returns 78 bytes
 * of ZSCII codes (26 each for A0, A1, A2) or null if the header slot is zero.
 */
export function readAlphabetTable(mem: Memory): Uint8Array | null {
	const addr = mem.getu(ALPHABET_TABLE_PTR);
	if (addr === 0) return null;
	return mem.bytes.slice(addr, addr + ALPHABET_TABLE_BYTES);
}

/** Translate a raw ZSCII code to a single Unicode character. */
export function zsciiToChar(code: number, zsciiExtra: string = DEFAULT_ZSCII_EXTRA): string {
	if (code === 13) return '\n';
	if (code === 0) return '';
	if (code >= ZSCII_EXTRA_MIN && code <= ZSCII_EXTRA_MAX) {
		return zsciiExtra[code - ZSCII_EXTRA_MIN] ?? '';
	}
	return String.fromCharCode(code);
}

/**
 * Inverse of `zsciiToChar` — used when writing to a memory stream, where the
 * Z-machine expects raw ZSCII bytes rather than Unicode codepoints. Unmapped
 * codepoints pass through (the caller masks to a byte).
 */
export function charToZscii(code: number, zsciiExtra: string = DEFAULT_ZSCII_EXTRA): number {
	if (code === 10) return 13;
	// ASCII and low-ZSCII fast path: the custom range starts at U+00A0 (the
	// lowest codepoint in ZSCII_EXTRA), so anything below that can't be remapped.
	if (code < 0xa0) return code;
	for (let i = 0; i < zsciiExtra.length; i++) {
		if (zsciiExtra.charCodeAt(i) === code) return 155 + i;
	}
	return code;
}

export interface DecodedText {
	/** The decoded text. */
	text: string;
	/** Address of the first byte past the last Z-word. */
	end: number;
}

/**
 * Decode Z-encoded text starting at `addr`.
 * @param fwords address of the abbreviation table (for shift-5 abbreviations).
 * @param zsciiExtra 155..223 Unicode translation; defaults to the Z-spec default.
 * @param alphabet v5+ custom 78-byte alphabet table (§3.5.5); null uses defaults.
 */
export function decodeText(
	mem: Memory,
	fwords: number,
	addr: number,
	zsciiExtra: string = DEFAULT_ZSCII_EXTRA,
	alphabet: Uint8Array | null = null,
): DecodedText {
	let o = '';
	let ps = 0; // permanent shift
	let ts = 0; // temporary shift
	let y = 0; // aux state

	const d = (v: number): void => {
		if (ts === 3) {
			y = v << 5;
			ts = 4;
		} else if (ts === 4) {
			y += v;
			// Inline the ASCII fast path; only cold cases need the full translator.
			if (y === 13) o += '\n';
			else if (y > 0 && y < 155) o += String.fromCharCode(y);
			else if (y) o += zsciiToChar(y, zsciiExtra);
			ts = ps;
		} else if (ts === 5) {
			o += decodeText(mem, fwords, mem.getu(fwords + (y + v) * 2) * 2, zsciiExtra, alphabet)
				.text;
			ts = ps;
		} else if (v === 0) {
			o += ' ';
		} else if (v < 4) {
			ts = 5;
			y = (v - 1) * 32;
		} else if (v < 6) {
			if (!ts) ts = v - 3;
			else if (ts === v - 3) ps = ts;
			else ps = ts = 0;
		} else if (v === 6 && ts === 2) {
			ts = 3;
		} else if (alphabet) {
			o += zsciiToChar(alphabet[ts * 26 + v - 6]!, zsciiExtra);
			ts = ps;
		} else {
			o += ALPHABET[ts * 26 + v - 6];
			ts = ps;
		}
	};

	for (;;) {
		const w = mem.getu(addr);
		addr += 2;
		d((w >> 10) & 31);
		d((w >> 5) & 31);
		d(w & 31);
		if (w & 32768) break;
	}
	return { text: o, end: addr };
}
