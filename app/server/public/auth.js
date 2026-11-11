(function () {
  "use strict";
  const key = "s2-session";
  // 优先长期保存（localStorage）。有些 App 内置浏览器禁用了它：退到当前标签页的 sessionStorage，
  // 登录照样能进，只是关掉页面后要重新输入口令。两处都存不进去才算失败。
  window.S2Auth = {
    get() {
      try {
        const saved = localStorage.getItem(key);
        if (saved) return saved;
      } catch (_) {}
      try {
        const tab = sessionStorage.getItem(key);
        if (tab) {
          try { localStorage.setItem(key, tab); sessionStorage.removeItem(key); } catch (_) {}
          return tab;
        }
      } catch (_) {}
      return "";
    },
    set(token) {
      if (!token) { this.clear(); return; }
      try {
        localStorage.setItem(key, token);
        try { sessionStorage.removeItem(key); } catch (_) {}
        return;
      } catch (_) {}
      sessionStorage.setItem(key, token);
    },
    clear() {
      try { localStorage.removeItem(key); } catch (_) {}
      try { sessionStorage.removeItem(key); } catch (_) {}
    }
  };
})();
