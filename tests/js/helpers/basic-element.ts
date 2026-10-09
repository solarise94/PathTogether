/** Minimal inert element for auth API tests, not a DOM implementation. */
export function fakeEl() {
	const attributes = new Map<string, string>();
	return {
		hidden: true,
		textContent: "",
		innerHTML: "",
		value: "",
		disabled: false,
		style: {},
		classList: { add() {}, remove() {}, contains() { return false; } },
		setAttribute(name: string, value: string) { attributes.set(name, String(value)); },
		getAttribute(name: string) { return attributes.get(name) ?? null; },
		appendChild() {},
		addEventListener() {},
	};
}
