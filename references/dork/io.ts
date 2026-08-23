/**
 * Timer info passed with `read` / `read_char` on v4+. The IO should cancel the
 * pending read after `tenths` tenths of a second, returning `{cancelled: true}`.
 * Full routine-callback timers (where the spec allows the game's routine to
 * decide on each tick whether to cancel) require reentrant opcode dispatch and
 * aren't implemented yet — we surface cancellation on timeout instead.
 */
export interface ReadTimer {
	tenths: number;
}

export interface ReadResult {
	text: string;
	/** v4+: true when a timer cancelled the read. Always false otherwise. */
	cancelled: boolean;
}

/** Normalize the two legal return shapes of `read` to a single object. */
export function normalizeRead(r: string | ReadResult): ReadResult {
	return typeof r === 'string' ? { text: r, cancelled: false } : r;
}

export interface ZMachineIO {
	print(text: string, scripting: boolean): Promise<void> | void;
	/** Return plain text for a completed line, or a `ReadResult` to signal cancellation. */
	read(maxlen: number, timer?: ReadTimer): Promise<string | ReadResult> | string | ReadResult;
	save?(buf: Uint8Array): Promise<boolean> | boolean;
	restore?(): Promise<Uint8Array | null | undefined> | Uint8Array | null | undefined;
	restarted?(): Promise<void> | void;
	highlight?(fixpitch: boolean): Promise<void> | void;

	/** v3 auto-drawn status line. When defined, the header NO-STATUS flag stays clear. */
	updateStatusLine?(text: string, v18: number, v17: number): Promise<void> | void;

	// v4+ windowing and styling.
	splitWindow?(lines: number): Promise<void> | void;
	setWindow?(window: number): Promise<void> | void;
	eraseWindow?(window: number): Promise<void> | void;
	eraseLine?(value: number): Promise<void> | void;
	setCursor?(y: number, x: number): Promise<void> | void;
	/** Returns 1-indexed [y, x] of the upper-window cursor. */
	getCursor?(): readonly [number, number];
	setTextStyle?(style: number): Promise<void> | void;
	bufferMode?(buffering: boolean): Promise<void> | void;
	/** v5+: foreground / background colour numbers per Z-machine standard 1.0 §8.3. 0/1 mean "current". */
	setColour?(fg: number, bg: number): Promise<void> | void;
}
