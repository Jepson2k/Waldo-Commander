/**
 * Keybindings focus detection module.
 *
 * Monitors focus state and notifies Python backend to enable/disable
 * global keyboard shortcuts when user is typing in editor or inputs.
 */
(function () {
  "use strict";

  // Unknown until the first poll, which always reports: the app may still
  // hold the state of a page that closed while an input had focus.
  let lastFocusState = null;
  let notifyCallback = null;

  /**
   * Check if focus is on an element that should block keybindings.
   * Returns true if keybindings should be disabled.
   */
  function shouldBlockKeybindings() {
    const activeElement = document.activeElement;
    if (!activeElement) return false;

    // Check for CodeMirror editor focus (uses contenteditable divs)
    if (activeElement.classList.contains("cm-content")) {
      return true;
    }

    // Check parent chain for CodeMirror container
    let el = activeElement;
    while (el) {
      if (el.classList && el.classList.contains("cm-editor")) {
        return true;
      }
      el = el.parentElement;
    }

    // Check for regular input/textarea focus
    const tagName = activeElement.tagName.toLowerCase();
    if (tagName === "input" || tagName === "textarea") {
      // Allow keybindings for certain input types that don't need typing
      const inputType = activeElement.type?.toLowerCase();
      if (
        inputType === "checkbox" ||
        inputType === "radio" ||
        inputType === "button" ||
        inputType === "submit" ||
        inputType === "reset"
      ) {
        return false;
      }
      return true;
    }

    // Check for contenteditable elements
    if (activeElement.isContentEditable) {
      return true;
    }

    return false;
  }

  /**
   * Notify Python backend of focus state change.
   */
  function notifyFocusChange(shouldBlock) {
    if (shouldBlock !== lastFocusState) {
      lastFocusState = shouldBlock;
      if (notifyCallback) {
        notifyCallback(shouldBlock);
      }
    }
  }

  /**
   * Poll current focus state and notify if changed.
   */
  function pollFocusState() {
    const shouldBlock = shouldBlockKeybindings();
    notifyFocusChange(shouldBlock);
  }

  /**
   * Initialize the keybindings focus detection.
   * @param {Function} callback - Function to call with focus state (true = block keybindings)
   */
  function init(callback) {
    notifyCallback = callback;

    // Listen for focus changes
    document.addEventListener("focusin", pollFocusState);
    document.addEventListener("focusout", function () {
      // Small delay to allow focus to settle on new element
      setTimeout(pollFocusState, 50);
    });

    // Also poll on keydown as a fallback
    document.addEventListener(
      "keydown",
      function () {
        pollFocusState();
      },
      true
    );

    // Initial check after page loads
    setTimeout(pollFocusState, 500);

    console.log("[Keybindings] Focus detection initialized");
  }

  // Export to window for NiceGUI integration
  window.KeybindingsFocusDetector = {
    init: init,
    poll: pollFocusState,
    isBlocking: function () {
      return shouldBlockKeybindings();
    },
  };
})();
