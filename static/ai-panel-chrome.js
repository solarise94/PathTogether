/* Presentation adapter for the installed HistoPilot UI. The plugin owns session
 * selection, drafts, configuration and running tasks. Keep its controls and
 * dispatch their existing events; never duplicate the session state machine. */
(function () {
  'use strict';
  function init() {
    var $ = function (id) { return document.getElementById(id); };
    var panel = $('ai-panel'), toggle = $('ai-conversation-toggle');
    if (!panel || !toggle || panel.dataset.chromeReady) return;
    panel.dataset.chromeReady = 'true';
    var select = $('ai-session-select'), fresh = $('ai-fresh-btn');
    var recent = $('ai-conversations'), options = $('ai-options');
    var more = $('ai-options-toggle'), create = $('ai-new-conversation');
    var list = $('ai-conversation-list'), search = $('ai-conversation-search');
    var all = $('ai-conversation-all'), title = $('ai-panel-title');
    var config = $('ai-config-wrap'), collapsed = $('ai-config-collapsed');
    var settings = $('ai-service-settings'), settingsContent = $('ai-service-settings-content');
    var bar = $('ai-session-bar'), openPanel = null, opener = null, showAll = false;
    // Keep session actions inside their original bar: the plugin checks this
    // parent before inserting drawing/path controls on every list refresh.
    $('ai-session-tools').appendChild(bar);
    settingsContent.appendChild(collapsed);
    settingsContent.appendChild(config);
    panel.classList.add('ai-title-layout');
    var setup = document.createElement('button');
    setup.type = 'button'; setup.id = 'ai-setup-notice'; setup.className = 'ai-setup-notice'; setup.hidden = true;
    panel.insertBefore(setup, $('ai-trace'));
    setup.addEventListener('click', function () {
      if (openPanel !== options) open(options, more);
      settings.open = true; settings.querySelector('summary').focus();
    });
    function en() { return window.HP_I18N && window.HP_I18N.getLang() === 'en'; }
    function label(zh, english) { return en() ? english : zh; }
    function text(el, value) { if (el.textContent !== value) el.textContent = value; }
    function state() { return (window.HistoPilot && window.HistoPilot.s) || {}; }
    function metadata(option) {
      var found = null;
      (state().aiSessionListCache || []).some(function (item) {
        if (item.id === option.value) { found = item; return true; }
        return (item.branches || []).some(function (branch) {
          if (branch.id === option.value) { found = branch; return true; }
          return false;
        });
      });
      return found || {};
    }
    function name(option) {
      return metadata(option).title || option.textContent;
    }
    function close(restoreFocus) {
      recent.hidden = options.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      more.setAttribute('aria-expanded', 'false');
      openPanel = null;
      if (restoreFocus && opener) opener.focus();
    }
    function open(target, button) {
      if (openPanel === target) { close(true); return; }
      close(false);
      openPanel = target; opener = button; target.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      if (target === recent) { showAll = false; search.value = ''; renderList(); search.focus(); }
      else { settings.querySelector("summary").focus(); }
    }
    function renderList() {
      list.replaceChildren();
      var q = search.value.trim().toLocaleLowerCase();
      var rows = Array.from(select.options).filter(function (o) {
        return o.value && (!q || name(o).toLocaleLowerCase().includes(q));
      });
      all.hidden = !!q || showAll || rows.length <= 8;
      rows.slice(0, q || showAll ? rows.length : 8).forEach(function (o) {
        var b = document.createElement('button');
        b.type = 'button'; b.className = 'ai-session-row';
        b.setAttribute('aria-pressed', String(o.value === select.value));
        var caption = document.createElement('span'); caption.textContent = name(o);
        var meta = document.createElement('small');
        var item = metadata(o);
        var status = o.textContent.split(' · ').pop();
        var ts = item.updated_at || item.created_at;
        var date = ts ? new Date(typeof ts === 'number' ? ts * 1000 : ts) : null;
        meta.textContent = (item.kind === 'branch' ? label('分支 · ', 'Branch · ') : '') +
          (status === name(o) ? '' : status) +
          (date && !isNaN(date.getTime()) ? ' · ' + date.toLocaleDateString(en() ? 'en-US' : 'zh-CN', { month: 'short', day: 'numeric' }) : '');
        b.append(caption, meta);
        b.addEventListener('click', function () {
          select.value = o.value;
          select.dispatchEvent(new Event('change', { bubbles: true }));
          close(true); sync();
        });
        list.appendChild(b);
      });
      if (!rows.length) {
        var empty = document.createElement('p'); empty.className = 'ai-history-empty';
        empty.textContent = q ? label('没有匹配的对话', 'No matching conversations') : label('还没有对话，点击右上角开始', 'No conversations yet. Start one above.');
        list.appendChild(empty);
      }
    }
    function sync() {
      // Viewport attachment belongs with annotation tools, not the chat composer.
      // Older installed plugins still create this button during their init.
      var attachView = $('ai-attach-view-btn');
      if (attachView) attachView.remove();
      var selected = select.selectedOptions[0];
      var s = state();
      setup.hidden = !s.aiConfig || !window.HistoPilot.aiChannelConfigured || window.HistoPilot.aiChannelConfigured();
      text(setup, label('AI 尚未配置 · 查看设置', 'AI setup required · View settings'));
      text(title, s.aiDraft ? label('新对话', 'New conversation') : selected && selected.value ? name(selected) : label('AI 读片助手', 'AI assistant'));
      toggle.title = title.textContent;
      create.disabled = fresh.disabled || !s.slide;
      create.title = create.disabled ? label('请先打开切片', 'Open a slide first') : label('新对话', 'New conversation');
      create.setAttribute('aria-label', label('新对话', 'New conversation'));
      more.title = label('对话选项', 'Conversation options'); more.setAttribute('aria-label', more.title);
      text(recent.querySelector('.ai-popover-heading'), label('本切片的对话', 'Conversations for this slide'));
      text(options.querySelector('.ai-popover-heading'), more.title);
      text(settings.querySelector('summary'), label('AI 服务设置', 'AI service settings'));
      text(all, label('全部对话', 'All conversations'));
      search.placeholder = label('搜索对话', 'Search conversations'); search.setAttribute('aria-label', search.placeholder);
      // Hiding legacy new-conversation leaves aux empty in idle/running states.
      var cont = $('ai-continue-btn');
      $('ai-composer-aux').classList.toggle('ai-aux-empty', cont.style.display === 'none');
      if (openPanel === recent) renderList();
      if (panel.style.display === 'none') close(false);
    }
    toggle.addEventListener('click', function () { open(recent, toggle); });
    more.addEventListener('click', function () { open(options, more); });
    create.addEventListener('click', function () { fresh.click(); close(false); sync(); $('ai-task').focus(); });
    all.addEventListener('click', function () { showAll = true; renderList(); search.focus(); });
    search.addEventListener('input', renderList);
    select.addEventListener('change', sync);
    // Read-only observers bridge independent plugin updates (load, language,
    // restored draft, SSE and slide changes), without patching plugin methods.
    var observer = new MutationObserver(sync);
    observer.observe(config, { attributes: true, attributeFilter: ['style'] });
    observer.observe(select, { childList: true, subtree: true, characterData: true });
    observer.observe(fresh, { attributes: true, attributeFilter: ['disabled', 'style', 'title'] });
    observer.observe($('ai-continue-btn'), { attributes: true, attributeFilter: ['style'] });
    observer.observe(panel, { attributes: true, attributeFilter: ['style'] });
    document.addEventListener('hp-lang-change', sync);
    document.addEventListener('pointerdown', function (e) {
      if (openPanel && !openPanel.contains(e.target) && !opener.contains(e.target)) close(false);
    });
    panel.addEventListener('keydown', function (e) {
      if (!openPanel) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && openPanel === recent) {
        var buttons = Array.from(list.querySelectorAll('button'));
        if (!buttons.length) return;
        e.preventDefault();
        var index = buttons.indexOf(document.activeElement);
        var next = index < 0 ? (e.key === 'ArrowDown' ? 0 : buttons.length - 1)
          : (index + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
        buttons[next].focus();
      }
    });
    sync();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
