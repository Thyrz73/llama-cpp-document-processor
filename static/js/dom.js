// Small DOM helpers shared by the interface modules.

// Return the element with this ID, or null.
export const $ = (id) => document.getElementById(id);

// Create an element with optional text and CSS class.
// The text is set with textContent, so it is never interpreted as HTML.
export function el(tag, content, cls) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (cls) node.className = cls;
  return node;
}
