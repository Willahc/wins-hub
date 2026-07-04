(function () {
  function isAdminPath() {
    const p = window.location.pathname || "/";
    return p === "/admin" || p.startsWith("/admin/");
  }

  function findAvatarW(header) {
    const items = Array.from(header.querySelectorAll("button, a, div, span"));

    return items.find(function (el) {
      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const rect = el.getBoundingClientRect();

      return (
        text === "W" &&
        rect.width >= 28 &&
        rect.width <= 70 &&
        rect.height >= 28 &&
        rect.height <= 70 &&
        rect.left > window.innerWidth - 160
      );
    });
  }

  function removeOnlyBadgeBeforeAvatar() {
    if (!isAdminPath()) return;

    const header = document.querySelector("header:not(.wins-public-header)");
    if (!header) return;

    const avatar = findAvatarW(header);
    if (!avatar) return;

    const avatarRect = avatar.getBoundingClientRect();

    const candidates = Array.from(header.querySelectorAll("button, a, div, span"));

    candidates.forEach(function (el) {
      if (el === avatar || el.contains(avatar) || avatar.contains(el)) return;

      const text = (el.textContent || "").replace(/\s+/g, " ").trim();
      const rect = el.getBoundingClientRect();

      const isNearAvatar =
        rect.right <= avatarRect.left + 6 &&
        rect.right >= avatarRect.left - 90 &&
        Math.abs((rect.top + rect.height / 2) - (avatarRect.top + avatarRect.height / 2)) < 24;

      const isSmallBadge =
        rect.width >= 18 &&
        rect.width <= 85 &&
        rect.height >= 18 &&
        rect.height <= 45;

      const hasZero =
        text === "0" ||
        text.includes("0");

      if (isNearAvatar && isSmallBadge && hasZero) {
        el.remove();
      }
    });
  }

  function run() {
    removeOnlyBadgeBeforeAvatar();
  }

  document.addEventListener("DOMContentLoaded", run);
  setTimeout(run, 100);
  setTimeout(run, 500);
  setTimeout(run, 1200);
  setTimeout(run, 2500);

  const observer = new MutationObserver(function () {
    run();
  });

  document.addEventListener("DOMContentLoaded", function () {
    observer.observe(document.body, {
      childList: true,
      subtree: true
    });
  });
})();
