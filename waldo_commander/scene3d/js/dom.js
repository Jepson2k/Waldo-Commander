// DOM helpers for the cards and markers the view draws over itself. Writes go
// through these only when a value changes: the layout runs every frame, and
// an unchanged write still invalidates the browser's style.

export function element(tag, cls, parent, text = "") {
  const node = document.createElement(tag);
  node.className = cls;
  node.textContent = text;
  parent.append(node);
  return node;
}

export function setText(node, value) {
  if (node.textContent === value) return;
  if (node.firstChild?.nodeType === Node.TEXT_NODE && node.childNodes.length === 1) node.firstChild.data = value;
  else node.textContent = value;
}

export function setAttr(node, name, value) {
  if (node.getAttribute(name) !== value) node.setAttribute(name, value);
}

export function setStyle(node, name, value) {
  if (node.style[name] !== value) node.style[name] = value;
}

export function setHidden(node, hidden) {
  if (node.hidden !== hidden) node.hidden = hidden;
}
