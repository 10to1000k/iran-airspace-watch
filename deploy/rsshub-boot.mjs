// RSSHub entrypoint with a post-boot Instagram hashtag shim.
//
// RSSHub's dist/index.mjs overwrites globalThis.fetch during module evaluation,
// so a plain `node --import` preload is erased before any route runs. This
// wrapper imports RSSHub first and installs the shim afterwards, when
// globalThis.fetch is final. Used with the pinned 2026-11-05 image, whose
// dist/index.mjs contains the same startup behavior.
//
// Wired in deploy/social-feed-hub.yaml:
//   command: node --max-http-header-size=32768 /patch/rsshub-boot.mjs

await import('/app/dist/index.mjs');

const { installInstagramTagsPatch } = await import('/patch/instagram-tags.mjs');
installInstagramTagsPatch();
