(function () {
  if (window.location.pathname !== "/login") return;

  function removeTextFragment(fragment) {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const nodes = [];

    while (walker.nextNode()) {
      const node = walker.currentNode;
      if ((node.nodeValue || "").includes(fragment)) {
        nodes.push(node);
      }
    }

    nodes.forEach(function (node) {
      node.nodeValue = node.nodeValue.replace(fragment, "").trim();
    });
  }

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

      if (text.includes("Começar grátis") || text.includes("Comecar gratis")) {
        el.remove();
      }
    });

    removeTextFragment("Não tem conta?");
    removeTextFragment("Nao tem conta?");
  }

  document.addEventListener("DOMContentLoaded", cleanLoginHeader);
  setTimeout(cleanLoginHeader, 100);
  setTimeout(cleanLoginHeader, 500);
  setTimeout(cleanLoginHeader, 1200);
})();
