(() => {
  "use strict";

  const body = document.body;
  const liveRegion = document.getElementById("studio-live-region");

  body.addEventListener("htmx:beforeSwap", (event) => {
    const status = event.detail.xhr.status;
    if (status >= 400 && status < 600) {
      // UI partial routes return accessible repair cards with truthful HTTP
      // statuses. Allow HTMX to place those cards instead of hiding them.
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  body.addEventListener("htmx:beforeRequest", (event) => {
    const trigger = event.detail.elt;
    const button = trigger.matches("form")
      ? trigger.querySelector('button[type="submit"]')
      : trigger.closest("form")?.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
  });

  body.addEventListener("htmx:afterRequest", (event) => {
    const trigger = event.detail.elt;
    const button = trigger.matches("form")
      ? trigger.querySelector('button[type="submit"]')
      : trigger.closest("form")?.querySelector('button[type="submit"]');
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });

  body.addEventListener("htmx:afterSwap", (event) => {
    const focusTarget = event.detail.target.querySelector("[data-autofocus]");
    if (focusTarget) {
      focusTarget.focus({ preventScroll: true });
      focusTarget.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    if (liveRegion) {
      liveRegion.textContent = "页面内容已更新。";
    }
  });
})();
