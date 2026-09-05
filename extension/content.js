/**
 * Ghost Bridge — Content Script
 *
 * Injected into every page. Handles commands that need direct DOM access
 * beyond what chrome.scripting.executeScript provides (hover states,
 * mutation observers, etc.)
 */

// Listen for messages from the background service worker
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type !== "ghost-content-command") return false;

  try {
    const result = handleContentCommand(msg.command, msg.args || {});
    sendResponse({ result });
  } catch (err) {
    sendResponse({ error: err.message });
  }
  return false;
});

function handleContentCommand(command, args) {
  switch (command) {
    case "highlight":
      return highlightElement(args);
    case "get-selection":
      return { text: window.getSelection().toString() };
    case "scroll-to":
      return scrollToElement(args);
    default:
      return { error: `Unknown content command: ${command}` };
  }
}

function highlightElement(args) {
  // Remove previous highlights
  document.querySelectorAll("[data-ghost-highlight]").forEach(el => {
    el.style.outline = el.dataset.ghostOutline || "";
    el.removeAttribute("data-ghost-highlight");
    el.removeAttribute("data-ghost-outline");
  });

  if (!args.selector && args.choice === undefined) return { cleared: true };

  const el = args.selector
    ? document.querySelector(args.selector)
    : document.querySelector(`[data-ghost-id="${args.choice}"]`);

  if (!el) return { error: "Element not found" };

  el.dataset.ghostOutline = el.style.outline;
  el.dataset.ghostHighlight = "true";
  el.style.outline = "3px solid #6366f1";
  el.scrollIntoView({ behavior: "smooth", block: "center" });

  return { highlighted: true };
}

function scrollToElement(args) {
  const el = args.selector
    ? document.querySelector(args.selector)
    : document.querySelector(`[data-ghost-id="${args.choice}"]`);

  if (!el) return { error: "Element not found" };

  el.scrollIntoView({ behavior: "smooth", block: "center" });
  return { scrolled: true };
}
