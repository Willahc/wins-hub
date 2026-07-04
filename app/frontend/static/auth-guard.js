(function () {
  function hasAuthToken() {
    try {
      const keys = Object.keys(localStorage).concat(Object.keys(sessionStorage));
      return keys.some(function (k) {
        const v = localStorage.getItem(k) || sessionStorage.getItem(k) || "";
        return /token|jwt|auth|wins/i.test(k) && String(v).length > 20;
      });
    } catch (e) {
      return false;
    }
  }

  function isProtectedPath() {
    const p = window.location.pathname || "/";
    return (
      p === "/app" ||
      p === "/projetos" ||
      p.startsWith("/projetos/") ||
      p === "/matches" ||
      p.startsWith("/matches/") ||
      p === "/fornecedores" ||
      p.startsWith("/fornecedores/") ||
      p === "/admin" ||
      p.startsWith("/admin/") ||
      p === "/vendas" ||
      p.startsWith("/vendas/") ||
      p === "/admin-vendas" ||
      p.startsWith("/admin-vendas/")
    );
  }

  function hideAppNavigationWhenAnonymous() {
    if (hasAuthToken()) return;

    const style = document.createElement("style");
    style.setAttribute("data-wins-auth-guard", "1");
    style.textContent = `
      body:not(.wins-auth-ok) aside,
      body:not(.wins-auth-ok) .sidebar,
      body:not(.wins-auth-ok) [class*="sidebar"],
      body:not(.wins-auth-ok) nav a[href="/projetos"],
      body:not(.wins-auth-ok) nav a[href="/matches"],
      body:not(.wins-auth-ok) nav a[href="/fornecedores"],
      body:not(.wins-auth-ok) a[href="/admin"],
      body:not(.wins-auth-ok) a[href="/vendas"],
      body:not(.wins-auth-ok) a[href="/admin-vendas"] {
        display: none !important;
      }
    `;
    document.head.appendChild(style);
  }

  function guard() {
    if (hasAuthToken()) {
      document.body.classList.add("wins-auth-ok");
      return;
    }

    hideAppNavigationWhenAnonymous();

    if (isProtectedPath()) {
      window.location.replace("/login");
    }
  }

  document.addEventListener("click", function (ev) {
    const a = ev.target.closest && ev.target.closest("a");
    if (!a || hasAuthToken()) return;

    const href = a.getAttribute("href") || "";
    if (
      href === "/projetos" ||
      href.startsWith("/projetos") ||
      href === "/matches" ||
      href.startsWith("/matches") ||
      href === "/fornecedores" ||
      href.startsWith("/fornecedores") ||
      href === "/admin" ||
      href.startsWith("/admin") ||
      href === "/vendas" ||
      href.startsWith("/vendas")
    ) {
      ev.preventDefault();
      window.location.href = "/login";
    }
  }, true);

  const originalPushState = history.pushState;
  const originalReplaceState = history.replaceState;

  history.pushState = function () {
    originalPushState.apply(history, arguments);
    setTimeout(guard, 0);
  };

  history.replaceState = function () {
    originalReplaceState.apply(history, arguments);
    setTimeout(guard, 0);
  };

  window.addEventListener("popstate", guard);
  document.addEventListener("DOMContentLoaded", guard);
  setTimeout(guard, 100);
  setTimeout(guard, 500);
})();
