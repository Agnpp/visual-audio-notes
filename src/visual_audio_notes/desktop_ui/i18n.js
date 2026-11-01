/* Shared application labels only. Never translate source materials or notes. */
(() => {
  'use strict';
  window.VAN_I18N = {
    language: window.VAN_LANGUAGE === 'en' ? 'en' : 'zh-CN',
    async init() {
      const response = await fetch('/translations.json');
      if (!response.ok) throw new Error('Language resources are missing. Extract the entire app folder again.');
      const catalog = await response.json(), english = this.language === 'en';
      this.t = (text) => english && Object.hasOwn(catalog, text) ? catalog[text] : text;
      document.documentElement.lang = this.language;
      // Run once, before projects are rendered. No observer touches user text.
      const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
      for (let node; (node = walker.nextNode());) {
        if (['SCRIPT', 'STYLE'].includes(node.parentElement?.tagName)) continue;
        const source = node.nodeValue.trim();
        if (Object.hasOwn(catalog, source)) node.nodeValue = node.nodeValue.replace(source, this.t(source));
      }
      for (const node of document.querySelectorAll('[title], [placeholder], [aria-label]')) {
        for (const name of ['title', 'placeholder', 'aria-label']) {
          const source = node.getAttribute(name);
          if (source !== null) node.setAttribute(name, this.t(source));
        }
      }
      return this;
    },
  };
})();
