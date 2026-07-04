(function () {
  if (window.location.pathname !== "/login") return;

  function cleanLoginHeader() {
    document.body.classList.add("login-page");

    const forbiddenTopTexts = [
      "Como Funciona",
      "Ver Planos",
      "Entrar",
      "Começar grátis",
      "Comecar gratis"
    ];

    document.querySelectorAll("a, button").forEach(function (el) {
      const text = (el.textContent || "").trim();

      const rect = el.getBoundingClientRect();
      const isTopButton = rect.top >= 0 && rect.top < 180;

      if (isTopButton && forbiddenTopTexts.some(t => text.includes(t))) {
        el.remove();
      }

      if (text.includes("Não tem conta") || text.includes("Começar grátis") || text.includes("Comecar gratis")) {
        const parent = el.closest("div,p,span");
        if (parent) parent.remove();
        else el.remove();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", cleanLoginHeader);
  setTimeout(cleanLoginHeader, 100);
  setTimeout(cleanLoginHeader, 500);
  setTimeout(cleanLoginHeader, 1200);
})();
