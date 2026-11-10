(function () {
  "use strict";
  const form = document.querySelector("form");
  const error = document.getElementById("login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const password = document.getElementById("tk");
    error.hidden = true;
    button.disabled = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/login", {
        method: "POST", credentials: "omit", cache: "no-store", signal: controller.signal,
        body: new URLSearchParams(new FormData(form)),
      });
      const data = await response.json();
      password.value = "";
      if (!response.ok || !data || data.ok !== true || typeof data.session !== "string") {
        throw new Error(data && typeof data.msg === "string" ? data.msg : "登录失败，请重试");
      }
      try {
        sessionStorage.removeItem("s2-session");
        if (data.session) sessionStorage.setItem("s2-session", data.session);
      } catch (_) {
        throw new Error("无法保存登录状态，请允许本站使用浏览器存储后重试");
      }
      location.replace("/");
    } catch (e) {
      error.textContent = e.name === "AbortError" ? "设备响应超时，请重试"
        : e instanceof TypeError || e instanceof SyntaxError ? "无法连接设备，请稍后重试" : e.message;
      error.hidden = false;
    } finally {
      clearTimeout(timer);
      button.disabled = false;
    }
  });
})();
