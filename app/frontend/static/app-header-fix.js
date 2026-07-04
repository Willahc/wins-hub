(function () {
  const publicPaths = ["/", "/como-funciona", "/planos", "/login", "/esqueci", "/reset"];

  function isPublicPath() {
    const p = window.location.pathname || "/";
    return publicPaths.includes(p);
  }

  function isAdminPath() {
    const p = window.location.pathname || "/";
    return p === "/admin" || p.startsWith("/admin/");
  }

  function markAppPage() {
    if (!isPublicPath()) {
      document.body.classList.add("wins-app-page");
    }

    if (isAdminPath()) {
      document.body.classList.add("wins-admin-page");
    }
  }

  document.addEventListener("DOMContentLoaded", markAppPage);
  setTimeout(markAppPage, 100);
  setTimeout(markAppPage, 500);
  setTimeout(markAppPage, 1200);
})();
