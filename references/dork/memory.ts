export class Memory {
	readonly bytes: Uint8Array;
	readonly view: DataView;
	readonly byteSwapped: boolean;

	constructor(bytes: Uint8Array, byteSwapped: boolean) {
		this.bytes = bytes;
		this.view = new DataView(bytes.buffer);
		this.byteSwapped = byteSwapped;
	}

	get(x: number): number {
		return this.view.getInt16(x, this.byteSwapped);
	}
	getu(x: number): number {
		return this.view.getUint16(x, this.byteSwapped);
	}
	put(x: number, y: number): void {
		this.view.setInt16(x, y, this.byteSwapped);
	}
	putu(x: number, y: number): void {
		this.view.setUint16(x, y & 65535, this.byteSwapped);
	}
}
