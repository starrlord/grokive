// background/background.js — Grokive Prompt Studio
// =============================================================================
// Message router + keyboard command + notifications.
//
// COUPLING: manifest.json lists `lib/api.js` BEFORE this file in
// background.scripts, so by the time this runs `self.GrokiveAPI` already exists.
// This file is a thin dispatcher — all settings, network, auth retry, the
// random pick, and clipboard logic live in lib/api.js (the single source of
// truth). We just translate runtime messages and the hotkey into GrokiveAPI
// calls.
//
// Why network lives in the background (CORS): the Grokive server sends no CORS
// headers, so popup/options/content pages cannot fetch it. The background page
// holds `<all_urls>` host permission and its fetches bypass CORS, so every
// surface routes its requests here via ext.runtime.sendMessage.
// =============================================================================

(function () {
  'use strict';

  // WebExtension promise-API shim.
  const ext = (typeof browser !== 'undefined') ? browser : chrome;
  const API = self.GrokiveAPI;

  // Notification icon (manifest SVG icon).
  const ICON_URL = ext.runtime.getURL('icons/icon.svg');

  // --- Message router ------------------------------------------------------
  // Every branch resolves to the {ok,data,error} envelope produced by
  // GrokiveAPI. Returning a Promise from onMessage keeps the channel open for
  // the async response (Firefox MV2 promise style).
  function handleMessage(msg) {
    if (!msg || typeof msg.type !== 'string') {
      return Promise.resolve({ ok: false, error: 'Bad message: missing type.' });
    }

    switch (msg.type) {
      case 'status':
        return API.status();

      case 'getResponses':
        return API.getResponses();

      case 'randomPrompt':
        return API.randomPrompt(msg.folder);

      case 'enhance':
        return API.enhance(msg.prompt, msg.dialogueLevel, msg.dialogueOnly);

      case 'generate':
        return API.generate(msg.prompt, msg.mode, msg.n, msg.instruction);

      case 'savePrompt':
        return API.savePrompt(msg.text, msg.folder, msg.starred);

      case 'starPrompt':
        return API.starPrompt(msg.id, msg.starred);

      case 'getSettings':
        return API.getSettings();

      case 'setSettings':
        return API.setSettings(msg.settings);

      case 'copyToClipboard':
        // Synchronous under the hood, but keep the Promise contract uniform.
        return Promise.resolve(API.copyToClipboard(msg.text));

      default:
        return Promise.resolve({ ok: false, error: 'Unknown message type: ' + msg.type });
    }
  }

  ext.runtime.onMessage.addListener((msg, _sender) => {
    // Always return a Promise so callers get the resolved envelope. Guard
    // against unexpected throws so the channel never dies silently.
    try {
      return Promise.resolve(handleMessage(msg)).catch((e) => ({
        ok: false,
        error: (e && e.message) ? e.message : String(e)
      }));
    } catch (e) {
      return Promise.resolve({ ok: false, error: (e && e.message) ? e.message : String(e) });
    }
  });

  // --- Notifications -------------------------------------------------------
  function notify(title, message) {
    try {
      ext.notifications.create({
        type: 'basic',
        iconUrl: ICON_URL,
        title: title,
        message: message
      });
    } catch (e) {
      // Notifications are best-effort; never throw out of a command.
    }
  }

  function truncate(text, max) {
    const t = (text || '').replace(/\s+/g, ' ').trim();
    if (t.length <= max) return t;
    return t.slice(0, max - 1).trimEnd() + '…';
  }

  // --- Keyboard command: 'random-prompt' (Alt+Shift+R) ---------------------
  // Pull a random prompt from settings.sourceFolder, copy it to the clipboard,
  // and show a notification with a truncated preview (or an error notification).
  async function runRandomPromptCommand() {
    try {
      const settings = await API.getSettingsRaw();
      const result = await API.randomPrompt(settings.sourceFolder);
      if (!result.ok) {
        notify('Grokive — Random prompt', result.error || 'Could not pull a prompt.');
        return;
      }
      const text = result.data.prompt.text || '';
      const copy = API.copyToClipboard(text);
      if (!copy.ok) {
        notify('Grokive — Random prompt', 'Pulled a prompt, but ' + (copy.error || 'clipboard copy failed') + '.');
        return;
      }
      notify('Grokive — Copied prompt', truncate(text, 160));
    } catch (e) {
      notify('Grokive — Random prompt', (e && e.message) ? e.message : String(e));
    }
  }

  if (ext.commands && ext.commands.onCommand) {
    ext.commands.onCommand.addListener((command) => {
      if (command === 'random-prompt') {
        runRandomPromptCommand();
      }
      // '_execute_browser_action' is handled natively by Firefox (opens popup).
    });
  }
})();
