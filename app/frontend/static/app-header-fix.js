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

  function removeAdminWalletBadge() {
    if (!isAdminPath()) return;

    const header = document.querySelector("header:not(.wins-public-header)");
    if (!header) return;

    header.querySelectorAll("button, a, div, span").forEach(function (el) {
      const text = (el.textContent || "").trim();
      const rect = el.getBoundingClientRect();

      const isSmallRightBadge =
        rect.width > 20 &&
        rect.width < 80 &&
        rect.height > 20 &&
        rect.height < 55 &&
        rect.left > window.innerWidth - 180;

      const looksLikeWallet =
        text === "0" ||
        text === "৳ 0" ||
        text.includes("0") && (
          el.innerHTML.includes("coin") ||
          el.innerHTML.includes("wallet") ||
          el.innerHTML.includes("credit") ||
          el.innerHTML.includes("ti-coin") ||
          el.innerHTML.includes("ti-wallet")
        );

      if (isSmallRightBadge && looksLikeWallet) {
        el.remove();
      }
    });
  }

  function markAppPage() {
    if (!isPublicPath()) {
      document.body.classList.add("wins-app-page");
    }

    if (isAdminPath()) {
      document.body.classList.add("wins-admin-page");
      removeAdminWalletBadge();
    }
  }

  document.addEventListener("DOMContentLoaded", markAppPage);
  setTimeout(markAppPage, 100);
  setTimeout(markAppPage, 500);
  setTimeout(markAppPage, 1200);
})();
