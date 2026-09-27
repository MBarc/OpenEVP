// app/ui/notes.js rendered with a tiny fake DOM (run by test_ui.py with Node): release
// notes in GitHub Markdown become paragraphs, lists, headings and bold/italic/code, and
// nothing in the notes is ever interpreted as HTML. Prints "ok" or throws.
"use strict";
const assert = require("assert");

class Node_ {
  constructor(tag, text) { this.tagName = tag; this.text = text; this.children = []; }
  appendChild(c) { if (c.tagName === "#fragment") this.children.push(...c.children); else this.children.push(c); return c; }
  get lastChild() { return this.children[this.children.length - 1]; }
  set textContent(t) { this.children = t ? [new Node_("#text", String(t))] : []; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map((c) => c.textContent).join(""); }
  // The rendered tree as compact markup, for comparison (text escaped so markup in notes shows).
  get html() {
    if (this.tagName === "#text") return this.text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const inner = this.children.map((c) => c.html).join("");
    return this.tagName === "#root" ? inner : `<${this.tagName.toLowerCase()}>${inner}</${this.tagName.toLowerCase()}>`;
  }
}
global.document = {
  createElement: (t) => new Node_(t.toUpperCase()),
  createTextNode: (t) => new Node_("#text", String(t)),
  createDocumentFragment: () => new Node_("#fragment"),
};
const { renderNotes } = require("../app/ui/notes.js");

function html(md) { const root = new Node_("#root"); renderNotes(root, md); return root.html; }

assert.strictEqual(html("Fixes for **EVP marks**. Update when it starts."),
  "<p>Fixes for <strong>EVP marks</strong>. Update when it starts.</p>");
assert.strictEqual(html("- **Browse folders** like File Explorer\n- *All recordings* switch\n\nDone."),
  "<ul><li><strong>Browse folders</strong> like File Explorer</li><li><em>All recordings</em> switch</li></ul><p>Done.</p>");
assert.strictEqual(html("## What's new\n1. one\n2. two"),
  "<h4>What's new</h4><ol><li>one</li><li>two</li></ol>");
assert.strictEqual(html("Run `openevp-st25.exe --wav` and see [the README](https://example.com/x)."),
  "<p>Run <code>openevp-st25.exe --wav</code> and see <span>the README</span>.</p>");
assert.strictEqual(html("line one\nline two"), "<p>line one line two</p>");
assert.strictEqual(html("Windows\r\n\r\n* item"), "<p>Windows</p><ul><li>item</li></ul>");
assert.strictEqual(html("- item\n  continues here"), "<ul><li>item continues here</li></ul>");
// Markup in the notes stays text; stray markers stay as they are.
assert.strictEqual(html("<img src=x onerror=alert(1)> **<b>bold</b>**"),
  "<p>&lt;img src=x onerror=alert(1)&gt; <strong>&lt;b&gt;bold&lt;/b&gt;</strong></p>");
assert.strictEqual(html("2 * 3 = 6 and a_b_c stays"), "<p>2 * 3 = 6 and a_b_c stays</p>");
assert.strictEqual(html(""), "");
console.log("ok");
