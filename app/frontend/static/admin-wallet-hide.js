(function () {
  function isAdminPath() {
    const p = window.location.pathname || "/";
    return p === "/admin" || p.startsWith("/admin/");
  }

  function rectOk(r) {
    return r && r.width > 0 && r.height > 0;
  }

  function findAvatarElement() {
    const nodes = Array.from(document.querySelectorAll("header button, header a, header div, header span"));

    return nodes.find(function (el) {
      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const r = el.getBoundingClientRect();

      return (
        text === "W" &&
        rectOk(r) &&
        r.left > window.innerWidth - 180 &&
        r.width >= 24 &&
        r.width <= 70 &&
        r.height >= 24 &&
        r.height <= 70
      );
    });
  }

  function removeBadgeImmediatelyBeforeAvatar() {
    if (!isAdminPath()) return;

    const avatar = findAvatarElement();
    if (!avatar) return;

    let avatarBox = avatar;

    // Sobe até o elemento clicável/container real do avatar, mas sem subir demais.
    for (let i = 0; i < 4; i++) {
      const p = avatarBox.parentElement;
      if (!p) break;

      const pr = p.getBoundingClientRect();
      const ar = avatar.getBoundingClientRect();

      const parentStillSmall =
        pr.width <= 120 &&
        pr.height <= 80 &&
        Math.abs((pr.top + pr.height / 2) - (ar.top + ar.height / 2)) < 20;

      if (parentStillSmall) {
        avatarBox = p;
      } else {
        break;
      }
    }

    // Tenta remover o irmão anterior direto.
    const parent = avatarBox.parentElement;
    if (parent) {
      const children = Array.from(parent.children);
      const idx = children.indexOf(avatarBox);

      if (idx > 0) {
        const prev = children[idx - 1];
        const text = (prev.textContent || "").replace(/\s+/g, " ").trim();
        const r = prev.getBoundingClientRect();
        const ar = avatarBox.getBoundingClientRect();

        const isSmall =
          rectOk(r) &&
          r.width <= 90 &&
          r.height <= 50;

        const isNear =
          r.right <= ar.left + 12 &&
          r.right >= ar.left - 110 &&
          Math.abs((r.top + r.height / 2) - (ar.top + ar.height / 2)) < 28;

        const isWallet =
          text.includes("0") ||
          prev.innerHTML.toLowerCase().includes("coin") ||
          prev.innerHTML.toLowerCase().includes("wallet") ||
          prev.innerHTML.toLowerCase().includes("credit") ||
          prev.innerHTML.toLowerCase().includes("currency");

        if (isSmall && isNear && isWallet) {
          prev.remove();
          return;
        }
      }
    }

    // Fallback: remove qualquer pequeno badge com 0 entre o avatar e 120px à esquerda.
    const ar = avatar.getBoundingClientRect();

    Array.from(document.querySelectorAll("header button, header a, header div, header span")).forEach(function (el) {
      if (el === avatar || el.contains(avatar) || avatar.contains(el)) return;

      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const r = el.getBoundingClientRect();

      const isSmall =
        rectOk(r) &&
        r.width >= 12 &&
        r.width <= 90 &&
        r.height >= 12 &&
        r.height <= 50;

      const isLeftOfAvatar =
        r.left > ar.left - 130 &&
        r.right < ar.left + 8 &&
        Math.abs((r.top + r.height / 2) - (ar.top + ar.height / 2)) < 30;

      const isZeroBadge =
        text.includes("0") &&
        !text.includes("Painel") &&
        !text.includes("Sair") &&
        !text.includes("William") &&
        !text.includes("Carregando");

      if (isSmall && isLeftOfAvatar && isZeroBadge) {
        el.remove();
      }
    });
  }

  function run() {
    removeBadgeImmediatelyBeforeAvatar();
  }

  document.addEventListener("DOMContentLoaded", run);
  window.addEventListener("load", run);
  setTimeout(run, 100);
  setTimeout(run, 500);
  setTimeout(run, 1200);
  setTimeout(run, 2500);
  setInterval(run, 1500);
})();
