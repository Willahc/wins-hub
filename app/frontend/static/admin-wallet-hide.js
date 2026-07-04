(function () {
  function isAdminPath() {
    const p = window.location.pathname || "/";
    return p === "/admin" || p.startsWith("/admin/");
  }

  function removeOnlyWalletZeroBadge() {
    if (!isAdminPath()) return;

    const header = document.querySelector("header:not(.wins-public-header)");
    if (!header) return;

    const candidates = Array.from(header.querySelectorAll("a, button, div, span"));

    candidates.forEach(function (el) {
      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const html = (el.innerHTML || "").toLowerCase();
      const rect = el.getBoundingClientRect();

      const looksLikeTinyBadge =
        rect.width >= 20 &&
        rect.width <= 70 &&
        rect.height >= 18 &&
        rect.height <= 40;

      const looksLikeWalletZero =
        (text === "0" || text === "$ 0" || text === "R$ 0" || text === "৳ 0" || text === "🪙 0") &&
        (
          html.includes("coin") ||
          html.includes("wallet") ||
          html.includes("credit") ||
          html.includes("ti-coin") ||
          html.includes("ti-wallet") ||
          html.includes("ti-currency")
        );

      // remove só o badge pequeno, nunca o avatar W
      if (looksLikeTinyBadge && looksLikeWalletZero) {
        el.remove();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", removeOnlyWalletZeroBadge);
  setTimeout(removeOnlyWalletZeroBadge, 100);
  setTimeout(removeOnlyWalletZeroBadge, 500);
  setTimeout(removeOnlyWalletZeroBadge, 1200);
})();
