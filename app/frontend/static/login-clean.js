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

  function cleanLoginOnlyHeader() {
    document.body.classList.add("login-page");

    // Remove apenas links/botões do HEADER, nunca botões dentro do formulário.
    const header = document.querySelector(".wins-public-header, header");

    if (header) {
      header.querySelectorAll("a, button").forEach(function (el) {
        const text = (el.textContent || "").trim();

        if (
          text.includes("Como Funciona") ||
          text.includes("Como funciona") ||
          text.includes("Ver Planos") ||
          text.includes("Planos") ||
          text.includes("Começar grátis") ||
          text.includes("Comecar gratis") ||
          text === "Entrar"
        ) {
          // Não remove o logo.
          if (!el.classList.contains("wins-brand")) {
            el.remove();
          }
        }
      });
    }

    // Remove só o texto residual do cadastro.
    removeTextFragment("Não tem conta?");
    removeTextFragment("Nao tem conta?");
  }

  document.addEventListener("DOMContentLoaded", cleanLoginOnlyHeader);
  setTimeout(cleanLoginOnlyHeader, 100);
  setTimeout(cleanLoginOnlyHeader, 500);
})();
