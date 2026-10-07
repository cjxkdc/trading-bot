import React, { useEffect, useRef } from 'react';

// showModal places the dialog in the browser's top layer and makes the rest
// of the document inert. Keep the original focus target across unmounts.
export function activateDialog(dialog) {
  const document = dialog.ownerDocument;
  const trigger = document.activeElement;
  const previousOverflow = document.body.style.overflow;
  dialog.showModal();
  document.body.style.overflow = 'hidden';
  return () => {
    if (dialog.open) dialog.close();
    document.body.style.overflow = previousOverflow;
    if (trigger?.isConnected && typeof trigger.focus === 'function') {
      trigger.focus({ preventScroll: true });
    }
  };
}

export function dismissDialog(event, canDismiss, onDismiss) {
  // Escape must never bypass a caller's disabled cancel action.
  event.preventDefault();
  if (canDismiss) onDismiss();
}

export function Dialog({ labelledBy, children, onDismiss, canDismiss = true }) {
  const ref = useRef(null);
  useEffect(() => activateDialog(ref.current), []);

  function onBackdropClick(event) {
    if (event.target !== ref.current) return;
    const bounds = ref.current.getBoundingClientRect();
    const outside = event.clientX < bounds.left || event.clientX > bounds.right ||
      event.clientY < bounds.top || event.clientY > bounds.bottom;
    if (outside) dismissDialog(event, canDismiss, onDismiss);
  }

  return <dialog ref={ref} className="modal" aria-modal="true" aria-labelledby={labelledBy}
    onCancel={event => dismissDialog(event, canDismiss, onDismiss)} onClick={onBackdropClick}>
    {children}
  </dialog>;
}
