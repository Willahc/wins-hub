/* ============================================================
   WiNS Hub — Alpine Stores Globais
   Lê o estado existente do localStorage (wnshub_token, wnshub_plano,
   wnshub_nome) pra não exigir migração de dados.
   ============================================================ */

(function () {
  'use strict';

  const TOKEN_KEY = 'wnshub_token';
  const PLANO_KEY = 'wnshub_plano';
  const NOME_KEY  = 'wnshub_nome';

  // Mapeia rótulo amigável por plano (mantém compat com backend que devolve UPPER).
  const PLANO_LABEL = {
    'GRATUITO':     'Gratuito',
    'ESSENCIAL':    'Essencial',
    'STANDARD':     'Standard',
    'PROFISSIONAL': 'Profissional',
    'ENTERPRISE':   'Enterprise',
  };

  function iniciais(nome) {
    if (!nome) return '';
    return nome
      .trim()
      .split(/\s+/)
      .slice(0, 2)
      .map(function (p) { return p[0]; })
      .join('')
      .toUpperCase();
  }

  function bootstrap() {
    if (!window.Alpine || !window.Alpine.store) {
      // Alpine ainda não carregou; tenta de novo no próximo tick.
      return false;
    }

    // ---------- auth ----------
    window.Alpine.store('auth', {
      loggedIn: false,
      plan: 'gratuito',          // normalizado em lowercase pra CSS class
      planRaw: 'GRATUITO',       // valor cru do backend
      planLabel: 'Gratuito',
      nome: '',
      initials: '',

      init() {
        const token = localStorage.getItem(TOKEN_KEY);
        const planoRaw = (localStorage.getItem(PLANO_KEY) || 'GRATUITO').toUpperCase();
        const nome = localStorage.getItem(NOME_KEY) || '';
        this.loggedIn = !!token;
        this.planRaw = planoRaw;
        this.plan = planoRaw.toLowerCase();
        this.planLabel = PLANO_LABEL[planoRaw] || planoRaw;
        this.nome = nome;
        this.initials = iniciais(nome);
      },

      get isPago() {
        return ['ESSENCIAL', 'STANDARD', 'PROFISSIONAL', 'ENTERPRISE'].includes(this.planRaw);
      },
      get canSeeDecisor() { return this.isPago; },
      get canSeeEmail() {
        return ['PROFISSIONAL', 'ENTERPRISE'].includes(this.planRaw);
      },
      get canSeePhone() {
        return this.planRaw === 'ENTERPRISE';
      },

      // 'public' (não logado) | 'free' (logado gratuito) | 'paid' (logado pago)
      estadoCard() {
        if (!this.loggedIn) return 'public';
        if (!this.isPago)  return 'free';
        return 'paid';
      },
    });

    // ---------- nav ----------
    window.Alpine.store('nav', {
      active: 'vitrine',
      set(name) { this.active = name; },
    });

    // ---------- modal ----------
    window.Alpine.store('modal', {
      current: null,
      open(name) { this.current = name; document.body.style.overflow = 'hidden'; },
      close()    { this.current = null; document.body.style.overflow = ''; },
      is(name)   { return this.current === name; },
    });

    // ---------- toasts ----------
    window.Alpine.store('toasts', {
      list: [],
      _seq: 0,
      add(message, type, icon) {
        type = type || 'default';
        icon = icon || 'ti-check';
        const id = ++this._seq;
        this.list.push({ id: id, message: message, type: type, icon: icon });
        setTimeout(() => this.remove(id), 4000);
        return id;
      },
      success(msg) { return this.add(msg, 'success', 'ti-check'); },
      error(msg)   { return this.add(msg, 'error',   'ti-alert-circle'); },
      info(msg)    { return this.add(msg, 'info',    'ti-info-circle'); },
      remove(id)   { this.list = this.list.filter(t => t.id !== id); },
    });

    return true;
  }

  // Alpine despacha 'alpine:init' antes de inicializar.
  document.addEventListener('alpine:init', function () {
    bootstrap();
  });

  // Fallback: se Alpine já carregou antes desse script.
  if (window.Alpine && window.Alpine.store) {
    bootstrap();
  }
})();
