import { Memory } from './memory.ts';
import {
	decodeText,
	zsciiToChar,
	charToZscii,
	readUnicodeTable,
	readAlphabetTable,
	DEFAULT_ZSCII_EXTRA,
} from './text.ts';
import { Vocabulary } from './vocab.ts';
import { serialize, deserialize, verify, type CallFrame } from './saves.ts';
import { normalizeRead, type ZMachineIO } from './io.ts';

export interface ZMachineOptions {
	isTandy?: boolean;
	seed?: number;
	/**
	 * Throw on unknown opcodes (default true). Turn off to run adversarial programs
	 * like crashme that deliberately feed garbage bytes — the interpreter silently
	 * no-ops unrecognized instructions instead.
	 */
	strict?: boolean;
	/**
	 * Safety cap on instructions executed before aborting with an error. Useful with
	 * `strict: false` to bound runaway random code. Default: no limit.
	 */
	maxInstructions?: number;
}

/**
 * Z-machine v3 interpreter. Story file goes in; calls into `io` come out.
 * Port of public-domain JSZM (by zzo38) to async/await + TypeScript.
 */
export type ZVersion = 3 | 4 | 5 | 7 | 8;

export class ZMachine {
	readonly memInit: Uint8Array;
	readonly version: ZVersion;
	readonly byteSwapped: boolean;
	readonly statusType: boolean;
	readonly serial: string;
	readonly zorkid: number;
	readonly isTandy: boolean;
	readonly strict: boolean;
	readonly maxInstructions: number;
	readonly io: ZMachineIO;

	private initialSeed?: number;
	private mem!: Memory;
	private savedFlags = 0;
	private seed = 0;
	private fwords = 0;
	private vocabulary: Vocabulary | null = null;
	private zsciiExtra: string = DEFAULT_ZSCII_EXTRA;
	private alphabet: Uint8Array | null = null;

	constructor(story: ArrayLike<number>, io: ZMachineIO, opts: ZMachineOptions = {}) {
		const bytes = new Uint8Array(story);
		this.memInit = bytes;
		const v = bytes[0];
		if (v !== 3 && v !== 4 && v !== 5 && v !== 7 && v !== 8) {
			throw new Error(`Unsupported Z-code version ${String(v)}.`);
		}
		this.version = v as ZVersion;
		this.byteSwapped = !!(bytes[1]! & 1);
		this.statusType = !!(bytes[1]! & 2);
		this.serial = String.fromCharCode(...bytes.slice(18, 24));
		this.zorkid =
			(bytes[2]! << (this.byteSwapped ? 0 : 8)) | (bytes[3]! << (this.byteSwapped ? 8 : 0));
		this.isTandy = !!opts.isTandy;
		this.initialSeed = opts.seed;
		this.strict = opts.strict !== false;
		this.maxInstructions = opts.maxInstructions ?? Infinity;
		this.io = io;
	}

	/** Checksum the loaded story against the header-declared value. */
	verify(): boolean {
		return verify(this.memInit, this.mem);
	}

	/**
	 * Decode text starting at `addr`. Convenience wrapper used by tooling;
	 * the opcode loop uses `decodeText` directly for the `end` return value.
	 */
	getText(addr: number): string {
		return decodeText(this.mem, this.fwords, addr, this.zsciiExtra, this.alphabet).text;
	}

	async run(): Promise<void> {
		let mem!: Memory;
		let bytes!: Uint8Array;
		let pc = 0;
		let cs: CallFrame[] = [];
		let ds: number[] = [];
		let op0 = 0,
			op1 = 0,
			op2 = 0,
			op3 = 0,
			op4 = 0,
			op5 = 0,
			op6 = 0,
			op7 = 0;
		let opc = 0;
		let op3Size = 0; // byte size of the property last located by propfind
		let globals = 0;
		let objects = 0;
		let defprop = 0;
		let endText = 0;

		// Object-table layout (v3: 9-byte entries, 32 attrs; v4: 14-byte entries, 48 attrs).
		const v3 = this.version === 3;
		const objSize = v3 ? 9 : 14;
		const attrBytes = v3 ? 4 : 6;
		const parentOff = v3 ? 4 : 6;
		const siblingOff = v3 ? 5 : 8;
		const childOff = v3 ? 6 : 10;
		const propAddrOff = attrBytes + (v3 ? 3 : 6);
		const defaultPropCount = v3 ? 31 : 63;

		const initRng = (): void => {
			this.seed =
				this.initialSeed !== undefined
					? this.initialSeed >>> 0
					: (Math.random() * 0xffffffff) >>> 0;
		};

		const init = (): void => {
			mem = this.mem = new Memory(new Uint8Array(this.memInit), this.byteSwapped);
			bytes = mem.bytes;
			if (v3) {
				bytes[1]! &= 3;
				if (this.isTandy) bytes[1]! |= 8;
				if (!this.io.updateStatusLine) bytes[1]! |= 16;
				if (this.io.splitWindow && this.io.setWindow) bytes[1]! |= 32;
			} else {
				// v4: bits 2/3/4 (bold/italic/fixed). v5+: also bit 0 (colour).
				bytes[1]! = this.version >= 5 ? 0b0001_1101 : 0b0001_1100;
				bytes[30] = 0; // interpreter number
				bytes[31] = 0; // interpreter version
				bytes[32] = 25; // screen height in lines
				bytes[33] = 80; // screen width in characters
				if (this.version >= 5) {
					mem.putu(34, 80); // screen width in units
					mem.putu(36, 25); // screen height in units
					bytes[38] = 1; // font width in units
					bytes[39] = 1; // font height in units
					bytes[44] = 9; // default background = white
					bytes[45] = 2; // default foreground = black
				}
				// v7 packs routine and string addresses with separate base offsets
				// (v6/v7 only; zero in v5/v8).
				routineOff = this.version === 7 ? mem.getu(40) * 8 : 0;
				stringsOff = this.version === 7 ? mem.getu(42) * 8 : 0;
				// v5+ may supply a custom ZSCII 155..223 translation via the header extension,
				// and/or a replacement alphabet table at header byte 0x34.
				if (this.version >= 5) {
					this.zsciiExtra = readUnicodeTable(mem) ?? DEFAULT_ZSCII_EXTRA;
					this.alphabet = readAlphabetTable(mem);
				}
			}
			mem.put(16, this.savedFlags);
			this.fwords = mem.getu(24);
			if (!this.vocabulary) {
				this.vocabulary = new Vocabulary(
					mem,
					this.fwords,
					mem.getu(8),
					this.zsciiExtra,
					this.alphabet,
				);
			}
			defprop = mem.getu(10) - 2;
			globals = mem.getu(12) - 32;
			cs = [];
			ds = [];
			pc = mem.getu(6);
			// objects[1] = objTableStart + defaultPropCount*2; objects + 1*objSize = that.
			objects = defprop + 2 + defaultPropCount * 2 - objSize;
			initRng();
		};

		const decode = (addr: number): string => {
			const r = decodeText(mem, this.fwords, addr, this.zsciiExtra, this.alphabet);
			endText = r.end;
			return r.text;
		};

		// Packed-address scaling:
		//   v3:   packed × 2
		//   v4-5: packed × 4
		//   v7:   packed × 4 + (per-type offset × 8); routines and strings have separate offsets
		//   v8:   packed × 8
		// v7 offsets are read from the header during init() and captured by reference.
		let routineOff = 0;
		let stringsOff = 0;
		const baseShift = this.version === 3 ? 1 : this.version === 8 ? 3 : 2;
		const basePack = (x: number): number => (x & 0xffff) << baseShift;
		const packedRoutine =
			this.version === 7 ? (x: number) => basePack(x) + routineOff : basePack;
		const packedString =
			this.version === 7 ? (x: number) => basePack(x) + stringsOff : basePack;

		const pcgetb = (): number => bytes[pc++]!;
		const pcget = (): number => {
			pc += 2;
			return mem.get(pc - 2);
		};
		const pcfetch = (): number => fetch(bytes[pc++]!);

		const fetch = (x: number): number => {
			if (x === 0) return ds.pop()!;
			if (x < 16) return cs[0]!.local[x - 1]!;
			return mem.get(globals + 2 * x);
		};

		const xfetch = (x: number): number => {
			if (x === 0) return ds[ds.length - 1]!;
			if (x < 16) return cs[0]!.local[x - 1]!;
			return mem.get(globals + 2 * x);
		};

		// Locals (Int16Array) and globals (setInt16) truncate on write; the data stack
		// is plain number[] so truncation has to be explicit to match Z-machine 16-bit
		// wraparound semantics (caught by Praxix's comarith test on 17-bit intermediates).
		const s16 = (y: number): number => (y << 16) >> 16;
		const xstore = (x: number, y: number): void => {
			if (x === 0) ds[ds.length - 1] = s16(y);
			else if (x < 16) cs[0]!.local[x - 1] = y;
			else mem.put(globals + 2 * x, y);
		};

		const store = (y: number): void => {
			const x = pcgetb();
			if (x === 0) ds.push(s16(y));
			else if (x < 16) cs[0]!.local[x - 1] = y;
			else mem.put(globals + 2 * x, y);
		};

		const ret = (x: number): void => {
			const frame = cs[0]!;
			ds = frame.ds;
			pc = frame.pc;
			cs.shift();
			// No-store call variants drop the return value instead of reading a store byte.
			if (!frame.discardResult) store(x);
		};

		const predicate = (p: boolean | number): void => {
			let x = pcgetb();
			const flip = !!(x & 128);
			const truthy = !!p;
			const take = flip ? !truthy : truthy;
			if (x & 64) x &= 63;
			else x = ((x & 63) << 8) | pcgetb();
			if (take) return;
			if (x === 0 || x === 1) return ret(x);
			if (x & 0x2000) x -= 0x4000;
			pc += x - 2;
		};

		// Object-field accessors. 1-byte in v3, 2-byte in v4+. Object 0 is "nothing";
		// per the Z-machine spec operations on it don't read/write the default-property
		// table that sits just before the object table, so the accessors short-circuit.
		const objField = (obj: number, off: number): number => {
			if (obj === 0) return 0;
			return v3
				? bytes[objects + obj * objSize + off]!
				: mem.getu(objects + obj * objSize + off);
		};
		const setObjField = (obj: number, off: number, val: number): void => {
			if (obj === 0) return;
			if (v3) bytes[objects + obj * objSize + off] = val;
			else mem.putu(objects + obj * objSize + off, val);
		};
		const getParent = (obj: number): number => objField(obj, parentOff);
		const getSibling = (obj: number): number => objField(obj, siblingOff);
		const getChild = (obj: number): number => objField(obj, childOff);
		const setParent = (obj: number, val: number): void => setObjField(obj, parentOff, val);
		const setSibling = (obj: number, val: number): void => setObjField(obj, siblingOff, val);
		const setChild = (obj: number, val: number): void => setObjField(obj, childOff, val);
		const getPropAddr = (obj: number): number =>
			obj === 0 ? 0 : mem.getu(objects + obj * objSize + propAddrOff);

		/**
		 * Prep FSET/FCLEAR/FSET? operands:
		 *   op2 = word address inside the object's attribute bits (opc holds its value);
		 *   op3 = the bit mask for the target attribute.
		 * Returns false for object 0 — caller should treat as no-op / false.
		 */
		const flagset = (): boolean => {
			if (op0 === 0) return false;
			op3 = 1 << (15 & ~op1);
			op2 = objects + op0 * objSize + (op1 >> 4) * 2;
			opc = mem.get(op2);
			return true;
		};

		/**
		 * Decode a property header at `header`, writing its fields into `pNum`/`pSize`/
		 * `pDataOff` (scratch vars shared across calls to avoid a tuple allocation in
		 * the propfind hot path).
		 * v3: 1-byte header `(size-1)<<5 | num`, num in 1..31, size in 1..8.
		 * v4: 1 or 2-byte header. If high bit set, 2-byte form: [0x80|num][0xC0|size]
		 *     (with size==0 meaning 64). Else 1-byte form: bit 6 ⇒ size=2, else size=1.
		 */
		let pNum = 0, pSize = 0, pDataOff = 0;
		const propLayout = (header: number): void => {
			const b1 = bytes[header]!;
			if (v3) {
				pNum = b1 & 31;
				pSize = (b1 >> 5) + 1;
				pDataOff = 1;
				return;
			}
			pNum = b1 & 0x3f;
			if (b1 & 0x80) {
				pSize = (bytes[header + 1]! & 0x3f) || 64;
				pDataOff = 2;
			} else {
				pSize = b1 & 0x40 ? 2 : 1;
				pDataOff = 1;
			}
		};

		/** Size of the property whose data starts at `dataAddr`. */
		const propSizeAt = (dataAddr: number): number => {
			const back1 = bytes[(dataAddr - 1) & 65535]!;
			if (v3) return (back1 >> 5) + 1;
			// v4: if top bit set, dataAddr-1 is the 2nd size byte, header is at dataAddr-2.
			propLayout((back1 & 0x80 ? dataAddr - 2 : dataAddr - 1) & 65535);
			return pSize;
		};

		const propfind = (): boolean => {
			if (op0 === 0) {
				op3 = 0;
				op3Size = 0;
				return false;
			}
			let z = getPropAddr(op0);
			z += bytes[z]! * 2 + 1; // skip short name (length prefix in words)
			while (bytes[z]) {
				propLayout(z);
				if (pNum === op1) {
					op3 = z + pDataOff;
					op3Size = pSize;
					return true;
				}
				z += pDataOff + pSize;
			}
			op3 = 0;
			op3Size = 0;
			return false;
		};

		const move = (x: number, y: number): void => {
			if (x === 0) return; // spec: no-op on object 0 (caught by strictz).
			let w = 0;
			let z: number;
			if ((z = getParent(x))) {
				if (getChild(z) === x) {
					setChild(z, getSibling(x));
				} else {
					z = getChild(z);
					while (z !== x) {
						w = z;
						z = getSibling(z);
					}
					setSibling(w, getSibling(x));
				}
			}
			setParent(x, y);
			if (y) {
				setSibling(x, getChild(y));
				setChild(y, x);
			} else {
				setSibling(x, 0);
			}
		};

		// Hot path: rebuild-avoiding dispatch array for operand-type codes 0/1/2.
		const opDispatch: Array<() => number> = [pcget, pcgetb, pcfetch];
		const opfetch = (x: number, y: number): number => {
			if ((x &= 3) === 3) return 0; // operand omitted; opc unchanged
			opc = y;
			return opDispatch[x]!();
		};

		// Shared CALL logic for the eight call variants.
		// `opc` must already reflect the total operand count (routine + args).
		// `storeResult=false` for the v5 call_vn / call_vn2 / call_1n / call_2n forms.
		const doCall = (storeResult: boolean): void => {
			if (!op0) {
				if (storeResult) store(0);
				return;
			}
			const fn = packedRoutine(op0);
			const localCount = bytes[fn]!;
			cs.unshift({
				ds,
				pc,
				local: new Int16Array(localCount),
				discardResult: !storeResult,
				argCount: opc - 1, // operand 0 is the routine; operands 1..opc-1 are args
			});
			ds = [];
			pc = fn + 1;
			const locals = cs[0]!.local;
			// v3/v4 routines store default local values in the header; v5+ they default to 0.
			if (this.version <= 4) {
				for (let i = 0; i < localCount; i++) locals[i] = pcget();
			}
			if (opc > 1 && localCount > 0) locals[0] = op1;
			if (opc > 2 && localCount > 1) locals[1] = op2;
			if (opc > 3 && localCount > 2) locals[2] = op3;
			if (opc > 4 && localCount > 3) locals[3] = op4;
			if (opc > 5 && localCount > 4) locals[4] = op5;
			if (opc > 6 && localCount > 5) locals[5] = op6;
			if (opc > 7 && localCount > 6) locals[6] = op7;
		};

		// Save / restore. Bodies are shared between v3/v4 SAVE/RESTORE (predicate-form,
		// opcodes 181/182) and v5 EXT:0/EXT:1 (store-form). The restore helper returns
		// the new (ds, cs, pc) tuple — the caller installs them since `let`-bound vars
		// can't be reassigned through a closure return.
		const doSave = async (): Promise<boolean> => {
			this.savedFlags = mem.get(16);
			return !!(this.io.save && (await this.io.save(serialize(mem, ds, cs, pc))));
		};
		const doRestore = async (): Promise<[number[], CallFrame[], number] | null> => {
			this.savedFlags = mem.get(16);
			const data = this.io.restore ? await this.io.restore() : null;
			const restored = data ? deserialize(mem, data) : null;
			mem.put(16, this.savedFlags);
			return restored;
		};

		// Output-stream 3: redirected prints go to a memory table instead of the screen.
		// A stack supports nesting (up to 16 levels per spec). Each entry stores the
		// table's base address and write cursor.
		const stream3: Array<{ base: number; cursor: number }> = [];

		// Push current room/score/moves into the host-drawn status line (v3 only path;
		// ignored if the game draws its own via split_window). Called before READ and
		// READ_CHAR, and as the USL opcode.
		const refreshStatus = async (): Promise<void> => {
			if (!this.io.updateStatusLine) return;
			await this.io.updateStatusLine(
				decode(getPropAddr(xfetch(16)) + 1),
				xfetch(18),
				xfetch(17),
			);
		};

		// Flush text; flip highlight when (flags & 2) changes. While output stream 3 is
		// active, text is diverted into a memory buffer rather than reaching the screen.
		const genPrint = async (text: string): Promise<void> => {
			if (stream3.length > 0) {
				const top = stream3[stream3.length - 1]!;
				// Memory streams want raw ZSCII, so reverse-translate the accented range.
				for (let i = 0; i < text.length; i++) {
					bytes[top.cursor++] = charToZscii(text.charCodeAt(i), this.zsciiExtra);
				}
				return;
			}
			const x = mem.get(16);
			if (x !== this.savedFlags) {
				this.savedFlags = x;
				if (this.io.highlight) await this.io.highlight(!!(x & 2));
			}
			await this.io.print(text, !!(x & 1));
		};

		// ─── init + restart hook ────────────────────────────────────────────
		init();
		if (this.io.restarted) await this.io.restarted();
		if (this.io.highlight) await this.io.highlight(!!(this.savedFlags & 2));

		// ─── main fetch/decode/execute loop ─────────────────────────────────
		// Only arm the counter when a finite cap is set — otherwise the per-iteration
		// increment + compare runs for free games and crosses the Smi→HeapNumber
		// boundary on long playthroughs.
		let insnCount = 0;
		const insnCap = this.maxInstructions;
		const capped = insnCap !== Infinity;
		for (;;) {
			if (capped && ++insnCount > insnCap) {
				throw new Error(`ZMachine: instruction cap exceeded (${insnCap})`);
			}
			let inst = pcgetb();
			if (inst < 128) {
				// 2OP
				if (inst & 64) op0 = pcfetch();
				else op0 = pcgetb();
				if (inst & 32) op1 = pcfetch();
				else op1 = pcgetb();
				inst &= 31;
				opc = 2;
			} else if (inst < 176) {
				// 1OP
				const x = (inst >> 4) & 3;
				inst &= 143;
				if (x === 0) op0 = pcget();
				else if (x === 1) op0 = pcgetb();
				else if (x === 2) op0 = pcfetch();
				opc = 1;
			} else if (inst === 190 && this.version >= 5) {
				// Extended-opcode prefix (v5+). The real opcode follows; operands like VAR.
				// Stash extended cases as 256+N in the switch.
				inst = 256 + pcgetb();
				const x = pcgetb();
				op0 = opfetch(x >> 6, 1);
				op1 = opfetch(x >> 4, 2);
				op2 = opfetch(x >> 2, 3);
				op3 = opfetch(x >> 0, 4);
			} else if (inst >= 192) {
				// EXT (VAR / 2OP long form). call_vs2 (236) and call_vn2 (250) have
				// TWO operand-types bytes up front, both read before any operand bytes.
				const x = pcgetb();
				const isDouble = inst === 236 || inst === 250;
				const y = isDouble ? pcgetb() : 0;
				op0 = opfetch(x >> 6, 1);
				op1 = opfetch(x >> 4, 2);
				op2 = opfetch(x >> 2, 3);
				op3 = opfetch(x >> 0, 4);
				if (isDouble) {
					op4 = opfetch(y >> 6, 5);
					op5 = opfetch(y >> 4, 6);
					op6 = opfetch(y >> 2, 7);
					op7 = opfetch(y >> 0, 8);
				}
				if (inst < 224) inst &= 31;
			}

			let x: number;
			switch (inst) {
				case 1: // EQUAL?
					predicate(op0 === op1 || (opc > 2 && op0 === op2) || (opc === 4 && op0 === op3));
					break;
				case 2:
					predicate(op0 < op1);
					break; // LESS?
				case 3:
					predicate(op0 > op1);
					break; // GRTR?
				case 4: // DLESS?
					x = s16(xfetch(op0) - 1);
					xstore(op0, x);
					predicate(x < op1);
					break;
				case 5: // IGRTR?
					x = s16(xfetch(op0) + 1);
					xstore(op0, x);
					predicate(x > op1);
					break;
				case 6: // IN? — parent(0) is 0 per accessor, so `jin 0 0` is true.
					predicate(getParent(op0) === op1);
					break;
				case 7:
					predicate((op0 & op1) === op1);
					break; // BTST
				case 8:
					store(op0 | op1);
					break; // BOR
				case 9:
					store(op0 & op1);
					break; // BAND
				case 10: // FSET? (test_attr)
					predicate(flagset() && !!(opc & op3));
					break;
				case 11: // FSET (set_attr)
					if (flagset()) mem.put(op2, opc | op3);
					break;
				case 12: // FCLEAR (clear_attr)
					if (flagset()) mem.put(op2, opc & ~op3);
					break;
				case 13:
					xstore(op0, op1);
					break; // SET
				case 14: // MOVE (insert_obj) — dest 0 means "remove from tree".
					move(op0, op1);
					break;
				case 15:
					store(mem.get((op0 + op1 * 2) & 65535));
					break; // GET
				case 16:
					store(bytes[(op0 + op1) & 65535]!);
					break; // GETB
				case 17: // GETP — propfind fails on obj 0, so default value is stored.
					if (propfind()) store(op3Size === 2 ? mem.get(op3) : bytes[op3]!);
					else store(mem.get(defprop + 2 * op1));
					break;
				case 18: // GETPT
					propfind();
					store(op3);
					break;
				case 19: // NEXTP
					if (op0 === 0) {
						store(0);
						break;
					}
					if (op1) {
						// Advance past the current property's data to the next header, return num.
						propfind();
						const after = op3 + op3Size;
						if (bytes[after] === 0) store(0);
						else {
							propLayout(after);
							store(pNum);
						}
					} else {
						// First property of object op0.
						x = getPropAddr(op0);
						const first = x + bytes[x]! * 2 + 1;
						if (bytes[first] === 0) store(0);
						else {
							propLayout(first);
							store(pNum);
						}
					}
					break;
				case 20:
					store(op0 + op1);
					break; // ADD
				case 21:
					store(op0 - op1);
					break; // SUB
				case 22:
					store(Math.imul(op0, op1));
					break; // MUL
				case 23:
					store(Math.trunc(op0 / op1));
					break; // DIV
				case 24:
					store(op0 % op1);
					break; // MOD

				// v5+ 2OP additions.
				case 26: // call_2n — call routine with 1 arg, no store
					doCall(false);
					break;
				case 27: // set_colour — fg=op0, bg=op1 (colour numbers per spec)
					if (this.io.setColour) await this.io.setColour(op0, op1);
					break;
				case 28: { // throw — return value op0 from frame number op1
					while (cs.length > op1) cs.shift();
					ret(op0);
					break;
				}

				case 128:
					predicate(!op0);
					break; // ZERO?
				case 129: // NEXT? (get_sibling)
					store((x = getSibling(op0)));
					predicate(x);
					break;
				case 130: // FIRST? (get_child)
					store((x = getChild(op0)));
					predicate(x);
					break;
				case 131: // LOC (get_parent)
					store(getParent(op0));
					break;
				case 132: // PTSIZE (get_prop_len)
					// Z-Machine 1.1: get_prop_len 0 must return 0 (caught by Praxix specfixes).
					store(op0 === 0 ? 0 : propSizeAt(op0));
					break;
				case 133:
					x = xfetch(op0);
					xstore(op0, x + 1);
					break; // INC
				case 134:
					x = xfetch(op0);
					xstore(op0, x - 1);
					break; // DEC
				case 135:
					await genPrint(decode(op0 & 65535));
					break; // PRINTB
				case 137: // REMOVE (remove_obj)
					move(op0, 0);
					break;
				case 138: // PRINTD (print_obj) — guard against obj 0 (getPropAddr returns 0).
					if (op0 !== 0) await genPrint(decode(getPropAddr(op0) + 1));
					break;
				case 139:
					ret(op0);
					break; // RETURN
				case 140:
					pc += op0 - 2;
					break; // JUMP
				case 141: // PRINT_PADDR — print string at a packed string address
					await genPrint(decode(packedString(op0)));
					break;
				case 142:
					store(xfetch(op0));
					break; // VALUE
				case 143: // v3-4: BCOM (~op0); v5: call_1n (call, no-store)
					if (this.version <= 4) store(~op0);
					else {
						opc = 1;
						doCall(false);
					}
					break;

				case 176:
					ret(1);
					break; // RTRUE
				case 177:
					ret(0);
					break; // RFALSE
				case 178: // PRINTI
					await genPrint(decode(pc));
					pc = endText;
					break;
				case 179: // PRINTR
					await genPrint(decode(pc) + '\n');
					ret(1);
					break;
				case 180:
					break; // NOOP
				case 181: // SAVE
					predicate(await doSave());
					break;
				case 182: { // RESTORE
					const r = await doRestore();
					if (r) {
						ds = r[0];
						cs = r[1];
						pc = r[2];
					}
					predicate(!!r);
					break;
				}
				case 183: // RESTART
					init();
					if (this.io.restarted) await this.io.restarted();
					break;
				case 184:
					ret(ds[ds.length - 1]!);
					break; // RSTACK
				case 185: // v3-4: FSTACK (pop top of stack); v5: CATCH (push current frame number)
					if (this.version <= 4) ds.pop();
					else store(cs.length);
					break;
				case 186:
					return; // QUIT
				case 187:
					await genPrint('\n');
					break; // CRLF
				case 188: // USL
					await refreshStatus();
					break;
				case 189:
					predicate(this.verify());
					break; // VERIFY
				case 191: // piracy (v5+) — branch if "genuine"; we always say yes
					predicate(true);
					break;

				case 25: // call_2s (2OP:25) — call routine with 1 arg, store result
				case 224: // call_vs / call (VAR:224)
				case 136: // call_1s (1OP:136) — call routine with no args, store result
				case 236: // call_vs2 (VAR:236) — call with up to 7 args, store result
					doCall(true);
					break;
				case 225:
					mem.put((op0 + op1 * 2) & 65535, op2);
					break; // PUT
				case 226:
					bytes[(op0 + op1) & 65535] = op2;
					break; // PUTB
				case 227: // PUTP
					propfind();
					if (op3Size === 2) mem.put(op3, op2);
					else bytes[op3] = op2;
					break;
				case 228: { // sread (v3/v4) / aread (v5: stores the terminator code)
					await genPrint('');
					await refreshStatus();
					// Operands 3 and 4 are v4+ timer: tenths of a second + routine.
					// The routine callback isn't invoked yet; we honour the timeout by cancelling.
					const tenths = opc > 2 ? op2 & 0xffff : 0;
					const { text, cancelled } = normalizeRead(
						await this.io.read(bytes[op0 & 65535]!, tenths > 0 ? { tenths } : undefined),
					);
					if (!cancelled) {
						this.vocabulary!.handleInput(
							mem,
							text,
							op0 & 65535,
							op1 & 65535,
							this.version,
						);
					}
					if (this.version >= 5) store(cancelled ? 0 : 13);
					break;
				}
				case 229: // PRINTC (print_char) — a raw ZSCII code (155-223 are accents).
					await genPrint(zsciiToChar(op0, this.zsciiExtra));
					break;
				case 230:
					await genPrint(String(op0));
					break; // PRINTN
				case 231: // RANDOM
					if (op0 <= 0) {
						if (op0 === 0) initRng();
						else this.seed = op0 >>> 0;
						store(0);
						break;
					}
					this.seed = (Math.imul(1664525, this.seed) + 1013904223) >>> 0;
					store(Math.floor((this.seed / 0xffffffff) * op0) + 1);
					break;
				case 232:
					ds.push(op0);
					break; // PUSH
				case 233:
					xstore(op0, ds.pop()!);
					break; // POP
				case 234: // split_window
					if (this.io.splitWindow) await this.io.splitWindow(op0);
					break;
				case 235: // set_window
					if (this.io.setWindow) await this.io.setWindow(op0);
					break;

				case 237: // erase_window
					if (this.io.eraseWindow) await this.io.eraseWindow(s16(op0));
					break;
				case 238: // erase_line
					if (this.io.eraseLine) await this.io.eraseLine(op0);
					break;
				case 239: // set_cursor y x
					if (this.io.setCursor) await this.io.setCursor(op0, op1);
					break;
				case 240: { // get_cursor → [y, x] written to table at op0
					const [y, x] = this.io.getCursor?.() ?? [1, 1];
					mem.putu(op0 & 65535, y);
					mem.putu((op0 + 2) & 65535, x);
					break;
				}
				case 241: // set_text_style
					if (this.io.setTextStyle) await this.io.setTextStyle(op0);
					break;
				case 242: // buffer_mode — 0 = unbuffered, non-zero = buffered
					if (this.io.bufferMode) await this.io.bufferMode(op0 !== 0);
					break;
				case 245: // sound_effect — not yet implemented
					break;

				case 243: { // output_stream
					const sid = op0 << 16 >> 16; // sign-extend to int16
					if (sid === 3) {
						// Open memory stream; op1 = table address. Reserve first 2 bytes for length.
						// Mask to unsigned 16 — operand comes in as int16 and tables live above 0x8000.
						const base = op1 & 0xffff;
						stream3.push({ base, cursor: base + 2 });
					} else if (sid === -3) {
						// Close top memory stream; write length back to the first 2 bytes.
						const top = stream3.pop();
						if (top) mem.putu(top.base, top.cursor - top.base - 2);
					}
					// Streams 1, 2, 4 not yet implemented; silently ignore.
					break;
				}

				case 244: // input_stream — no-op, we only read from the keyboard
					break;

				case 246: { // read_char — read a single keypress, return its ZSCII code
					await genPrint('');
					await refreshStatus();
					// Operands: (device, tenths, routine). Device is always 1 per spec.
					const tenths = opc > 1 ? op1 & 0xffff : 0;
					const { text, cancelled } = normalizeRead(
						await this.io.read(1, tenths > 0 ? { tenths } : undefined),
					);
					store(cancelled ? 0 : text.length > 0 ? text.charCodeAt(0) : 13);
					break;
				}

				case 247: { // scan_table
					// op0 = value, op1 = table, op2 = length, op3 = form (v5+; default 0x82)
					const form = opc >= 4 ? op3 : 0x82;
					const isWord = !!(form & 0x80);
					const entrySize = form & 0x7f;
					const needle = op0 & 0xffff;
					let found = 0;
					let a = op1 & 0xffff;
					for (let i = 0; i < op2; i++) {
						const v = isWord ? mem.getu(a) : bytes[a]!;
						if (v === needle) {
							found = a;
							break;
						}
						a = (a + entrySize) & 0xffff;
					}
					store(found);
					predicate(found);
					break;
				}

				// v5+ VAR opcodes.
				case 248: // not (moved here from 1OP:143)
					store(~op0);
					break;
				case 249: // call_vn — call with up to 3 args, no store
					doCall(false);
					break;
				case 250: // call_vn2 — call with up to 7 args, no store
					doCall(false);
					break;
				case 251: { // tokenise text parse [dict [flag]]
					const t1 = op0 & 0xffff;
					const len = bytes[(t1 + 1) & 0xffff]!;
					const slice = bytes.subarray(t1 + 2, t1 + 2 + len);
					this.vocabulary!.handleInput(
						mem,
						String.fromCharCode.apply(null, slice as unknown as number[]),
						t1,
						op1 & 0xffff,
						this.version,
					);
					break;
				}
				case 252: // encode_text — not yet implemented; write zeros so callers don't get garbage
					mem.put((op3 + 0) & 0xffff, 0);
					mem.put((op3 + 2) & 0xffff, 0);
					mem.put((op3 + 4) & 0xffff, 0);
					break;
				case 253: { // copy_table src, dst, size
					// Negative size = "always forward, no overlap check" per spec; copyWithin
					// happens to be a forward copy when source/dest don't overlap or when dst<src,
					// and a backward copy otherwise — exactly Z-machine memmove semantics.
					const src = op0 & 0xffff;
					const dst = op1 & 0xffff;
					const size = s16(op2);
					const n = Math.abs(size);
					if (dst === 0) bytes.fill(0, src, src + n);
					else if (size < 0) {
						// Forced forward copy regardless of overlap.
						for (let i = 0; i < n; i++) bytes[(dst + i) & 0xffff] = bytes[(src + i) & 0xffff]!;
					} else bytes.copyWithin(dst, src, src + n);
					break;
				}
				case 254: { // print_table text, width [height [skip]]
					const width = op1;
					const height = opc >= 3 ? op2 : 1;
					const skip = opc >= 4 ? op3 : 0;
					let p = op0 & 0xffff;
					for (let row = 0; row < height; row++) {
						// Translate ZSCII byte 13 to '\n'; render the rest as raw ASCII.
						const slice = bytes.subarray(p, p + width);
						let line = String.fromCharCode.apply(null, slice as unknown as number[]);
						if (line.includes('\r')) line = line.replace(/\r/g, '\n');
						await genPrint(row < height - 1 ? line + '\n' : line);
						p += width + skip;
					}
					break;
				}
				case 255: // check_arg_count N — branch if at least N args were given
					predicate(op0 <= cs[0]!.argCount);
					break;

				// Extended (0xBE) opcodes — switch dispatch is offset by +256.
				case 256: // ext:save (v5+: stores 1/0 instead of branching)
					store((await doSave()) ? 1 : 0);
					break;
				case 257: { // ext:restore (v5+: stores 2 on success, 0 on failure)
					const r = await doRestore();
					if (r) {
						ds = r[0];
						cs = r[1];
						pc = r[2];
					}
					store(r ? 2 : 0);
					break;
				}
				case 258: { // ext:log_shift  value, places (negative = right shift)
					const places = s16(op1);
					const u = op0 & 0xffff;
					store(places >= 0 ? (u << places) & 0xffff : u >>> -places);
					break;
				}
				case 259: { // ext:art_shift — arithmetic shift (sign-preserving for right)
					const places = s16(op1);
					const signed = s16(op0);
					store(places >= 0 ? (signed << places) & 0xffff : signed >> -places);
					break;
				}
				case 260: // ext:set_font — return previous font (1 = normal). We don't switch fonts.
					store(op0 === 0 ? 1 : op0 === 1 ? 1 : 0);
					break;
				case 265: // ext:save_undo — not implemented; per spec store -1
					store(-1);
					break;
				case 266: // ext:restore_undo
					store(-1);
					break;
				case 267: // ext:print_unicode — print a Unicode codepoint
					await genPrint(String.fromCodePoint(op0));
					break;
				case 268: // ext:check_unicode — return support flags. Bit 0 = can print, bit 1 = can read.
					store(0b01);
					break;

				default:
					if (this.strict) {
						throw new Error(
							`ZMachine: invalid opcode ${inst >= 256 ? 'EXT:' + (inst - 256) : inst} at pc=${pc - 1}`,
						);
					}
					// Robust mode: operand bytes are already consumed; treat the instruction
					// as a no-op and let the next byte be the next opcode. We can't know
					// whether the unknown opcode would have stored or branched without a
					// spec table, so we don't try to consume extra bytes.
					break;
			}
		}
	}
}
