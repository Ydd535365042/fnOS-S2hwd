(function () {
  "use strict";
  const key = "s2-session";
  window.S2Auth = {
    get() {
      try {
        const saved = localStorage.getItem(key);
        if (saved) return saved;
        const legacy = sessionStorage.getItem(key);
        if (legacy) { localStorage.setItem(key, legacy); sessionStorage.removeItem(key); return legacy; }
      } catch (_) {}
      return "";
    },
    set(token) {
      if (token) localStorage.setItem(key, token);
      else localStorage.removeItem(key);
      try { sessionStorage.removeItem(key); } catch (_) {}
    },
    clear() {
      try { localStorage.removeItem(key); } catch (_) {}
      try { sessionStorage.removeItem(key); } catch (_) {}
    }
  };
})();
