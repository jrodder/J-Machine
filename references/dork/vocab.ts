import type { Memory } from './memory.ts';
import { decodeText } from './text.ts';

/**
 * Built from the story file's dictionary header. Owns the word→address map
 * and the regex used to tokenize player input.
 */
export class Vocabulary {
	private readonly map: Map<string, number> = new Map();
	private readonly regBreak: RegExp;

	constructor(
		mem: Memory,
		fwords: number,
		s: number,
		zsciiExtra?: string,
		alphabet: Uint8Array | null = null,
	) {
		if (s === 0) {
			this.regBreak = new RegExp('[^ \\n\\t]+', 'g');
			return;
		}
		const sepCount = mem.bytes[s++]!;
		const seps = String.fromCharCode(...mem.bytes.slice(s, s + sepCount));
		const escapedSeps =
			seps
				.split('')
				.map((x) => (x.toUpperCase() === x.toLowerCase() ? '' : '\\') + x)
				.join('') + ']';
		this.regBreak = new RegExp('[' + escapedSeps + '|[^ \\n\\t' + escapedSeps + '+', 'g');
		s += sepCount;
		const entrySize = mem.bytes[s++]!;
		let n = mem.get(s);
		s += 2;
		while (n--) {
			this.map.set(decodeText(mem, fwords, s, zsciiExtra, alphabet).text, s);
			s += entrySize;
		}
	}

	/**
	 * Writes the player's command into the Z-machine's text and parse buffers.
	 * @param t1 text buffer address (max length at [t1]).
	 * @param t2 parse buffer address (max tokens at [t2]); 0 to skip tokenisation (v5+).
	 * @param version Z-code version — v5+ stores chars at offset 2 with a length byte at +1.
	 */
	handleInput(mem: Memory, str: string, t1: number, t2: number, version: number): void {
		str = str.toLowerCase().slice(0, mem.bytes[t1]! - 1);
		const textOffset = version >= 5 ? 2 : 1;
		for (let i = 0; i < str.length; i++) mem.bytes[t1 + textOffset + i] = str.charCodeAt(i);
		if (version >= 5) {
			mem.bytes[t1 + 1] = str.length;
		} else {
			mem.bytes[t1 + str.length + 1] = 0;
		}
		if (t2 === 0) return; // v5: parse buffer omitted means "don't tokenise"

		const truncate = (x: string): string => {
			let i = 0;
			return x
				.split('')
				.filter((y) => (i += /[a-z]/.test(y) ? 1 : /[0-9.,!?_#'"/\\:\-()]/.test(y) ? 2 : 4) < 7)
				.join('');
		};

		const tokens: Array<[number, number, number]> = [];
		const rx = new RegExp(this.regBreak.source, 'g');
		let m: RegExpExecArray | null;
		while ((m = rx.exec(str)) !== null) {
			const word = m[0];
			// Token offset is into the text buffer, which starts at `textOffset` past t1.
			tokens.push([word.length, this.map.get(truncate(word)) ?? 0, m.index + textOffset]);
		}

		let i = (mem.bytes[t2 + 1] = tokens.length);
		while (i--) {
			const entry = tokens[i]!;
			mem.putu(t2 + i * 4 + 2, entry[1]);
			mem.bytes[t2 + i * 4 + 4] = entry[0];
			mem.bytes[t2 + i * 4 + 5] = entry[2];
		}
	}
}
