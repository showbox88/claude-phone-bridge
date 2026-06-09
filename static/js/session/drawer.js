/**
 * Drawer open/close — responsive: desktop uses the grid-column body class
 * (`drawer-expanded`, persisted in localStorage), mobile uses the existing
 * `.drawer.hidden` overlay + mask.
 *
 * Ported from legacy app.js lines ~896-929.
 */
import { drawer, drawerMask } from '../dom.js';
import { loadSessionList } from './list.js';

const desktopDrawerMql = window.matchMedia('(min-width: 768px)');
const DRAWER_KEY = 'bridge.drawer_expanded';

export function isDesktopDrawer() { return desktopDrawerMql.matches; }

export function openDrawer() {
  if (isDesktopDrawer()) {
    document.body.classList.add('drawer-expanded');
    localStorage.setItem(DRAWER_KEY, '1');
  } else {
    if (drawer) drawer.classList.remove('hidden');
    if (drawerMask) drawerMask.classList.remove('hidden');
  }
  if (drawer) drawer.setAttribute('aria-hidden', 'false');
  loadSessionList();
}

export function closeDrawer() {
  if (isDesktopDrawer()) {
    document.body.classList.remove('drawer-expanded');
    localStorage.setItem(DRAWER_KEY, '0');
  } else {
    if (drawer) drawer.classList.add('hidden');
    if (drawerMask) drawerMask.classList.add('hidden');
  }
  if (drawer) drawer.setAttribute('aria-hidden', 'true');
}

export function toggleDrawer() {
  if (isDesktopDrawer()) {
    if (document.body.classList.contains('drawer-expanded')) closeDrawer();
    else openDrawer();
  } else {
    openDrawer();
  }
}
