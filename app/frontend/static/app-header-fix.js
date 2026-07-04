(function () {
  const publicPaths = ["/", "/como-funciona", "/planos", "/login", "/esqueci", "/reset"];

  function isPublicPath() {
    const p = window.location.pathname || "/";
    return publicPaths.includes(p);
  }

  function markAppPage() {
    if (!isPublicPath()) {
      document.body.classList.add("wins-app-page");
    }
  }

  document.addEventListener("DOMContentLoaded", markAppPage);
  setTimeout(markAppPage, 100);
  setTimeout(markAppPage, 500);
})();
