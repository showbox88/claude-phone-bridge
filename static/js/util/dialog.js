/**
 * Tiny <dialog>.showModal() fallback for iOS 14 / older Safari.
 *
 * Modern browsers (iOS 15+, Chrome, Firefox): uses native showModal/close.
 * Older browsers: toggles a `.dialog-open` class + locks body scroll +
 * sets aria-modal. CSS in dialogs/checkin.css (and any other dialog
 * CSS that uses <dialog>) styles the `:not([open]).dialog-open` state.
 *
 * Returns a `close` function. Callers use:
 *   const close = openDialog(el);
 *   // ...
 *   close();   // dismisses regardless of native vs fallback path
 *
 * Phase 4 Task 18.
 */

export function openDialog(el) {
  if (!el) return () => {};
  if (typeof el.showModal === 'function') {
    try {
      el.showModal();
      return () => { try { el.close(); } catch {} };
    } catch (e) {
      // showModal can throw if already open or detached — fall through
      // to the class-based path.
    }
  }
  // Fallback path
  el.classList.add('dialog-open');
  el.setAttribute('aria-modal', 'true');
  const prevBodyOverflow = document.body.style.overflow;
  document.body.style.overflow = 'hidden';
  return () => {
    el.classList.remove('dialog-open');
    el.removeAttribute('aria-modal');
    document.body.style.overflow = prevBodyOverflow;
  };
}
