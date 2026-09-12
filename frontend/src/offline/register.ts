/**
 * Service worker registration (design §8.1; N1, T8.1).
 *
 * Deliberately quiet. A failure to register means the app has no offline
 * support, which is a degradation — the online flows all still work — so it is
 * logged and swallowed rather than surfaced. A storekeeper cannot act on
 * "service worker registration failed" and should not be shown it.
 *
 * `autoUpdate` in the Vite config means a new deployment takes effect on the
 * next navigation. That is right for this product: a yard runs the app all day
 * in a browser tab, and an update prompt nobody taps is an app that stays six
 * versions behind.
 */

export async function registerServiceWorker(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) {
    return;
  }

  try {
    const { registerSW } = await import('virtual:pwa-register');
    registerSW({
      immediate: true,
      onRegisteredSW(url) {
        console.info('YardFlow offline support ready', url);
      },
      onRegisterError(error) {
        console.warn('YardFlow offline support unavailable', error);
      },
    });
  } catch (error) {
    // The virtual module does not exist when the plugin is disabled, which is
    // the case in unit tests and in any build that turns the PWA off.
    console.info('YardFlow running without offline support', error);
  }
}
