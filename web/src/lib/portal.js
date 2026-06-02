// Move a node to <body> so fixed-position overlays escape any ancestor that
// establishes a containing block (transform / filter / backdrop-filter — e.g.
// the glass top bar). Without this, `position: fixed` anchors to that ancestor.
export function portal(node) {
  document.body.appendChild(node);
  return {
    destroy() {
      if (node.parentNode) node.remove();
    }
  };
}
