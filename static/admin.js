/* ==========================================================================
   Панель администратора.

   Что здесь важно с точки зрения безопасности:
     * пароль нигде не сохраняется — он отправляется один раз на сервер и
       забывается; ни localStorage, ни cookie из скрипта не читаются;
     * cookie админа помечена HttpOnly, поэтому JS её не видит вообще;
     * все данные приходят только с сервера и только с валидной подписанной
       cookie; если её подделать, сервер ответит 401 и панель вернёт форму входа;
     * любое значение из базы выводится через textContent — вставить скрипт
       через фамилию студента не получится.
   ========================================================================== */

(function () {
  'use strict';

  var root = document.getElementById('app');

  var state = {
    session: null,
    summary: null,
    attempts: [],
    testFilter: '',
    tab: 'attempts',
    key: null,
    keyTest: '',
    log: [],
    detail: null,
    notices: [],
    ready: false
  };

  var REFRESH_MS = 20000;
  var timer = null;

  /* ------------------------------------------------------------- утилиты */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function api(path, options) {
    var opts = options || {};
    var init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-Admin-Request': '1', Accept: 'application/json' }
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          if (!response.ok) {
            var error = new Error((data && data.detail) || 'Ошибка запроса (' + response.status + ').');
            error.status = response.status;
            throw error;
          }
          return data;
        });
    });
  }

  function fmtDate(value) {
    if (!value) return '—';
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value);
    return date.toLocaleString('ru-RU', {
      day: '2-digit',
      month: '2-digit',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  function fmtDuration(seconds) {
    var total = Math.max(0, Math.round(Number(seconds) || 0));
    var minutes = Math.floor(total / 60);
    var rest = total % 60;
    return minutes + ':' + (rest < 10 ? '0' : '') + rest;
  }

  function notice(text) {
    state.notices = [text];
    return state.notices;
  }

  /* --------------------------------------------------------------- вход */

  function renderLogin(errorText) {
    if (timer) {
      window.clearInterval(timer);
      timer = null;
    }
    clear(root);

    var card = el('div', 'admin-login');
    card.appendChild(el('h1', 'admin-title', 'Вход в панель'));
    card.appendChild(el('p', 'admin-sub', 'Доступ только для преподавателя.'));

    var userField = el('label', 'admin-field');
    userField.appendChild(el('span', null, 'Логин'));
    var userInput = el('input', 'admin-input');
    userInput.type = 'text';
    userInput.name = 'username';
    userInput.autocomplete = 'off';
    userInput.spellcheck = false;
    userField.appendChild(userInput);

    var passField = el('label', 'admin-field');
    passField.appendChild(el('span', null, 'Пароль'));
    var passInput = el('input', 'admin-input');
    passInput.type = 'password';
    passInput.name = 'password';
    passInput.autocomplete = 'current-password';
    passField.appendChild(passInput);

    var button = el('button', 'btn btn--primary', 'Войти');
    button.type = 'button';
    button.style.width = '100%';

    var errorNode = el('div', 'admin-error', errorText || '');

    function submit() {
      var username = userInput.value.trim();
      var password = passInput.value;
      if (!username || !password) {
        errorNode.textContent = 'Введите логин и пароль.';
        return;
      }
      button.disabled = true;
      errorNode.textContent = '';
      api('/admin/api/login', { method: 'POST', body: { username: username, password: password } })
        .then(function () {
          passInput.value = '';
          return boot();
        })
        .catch(function (error) {
          errorNode.textContent = error.message;
          passInput.value = '';
          passInput.focus();
        })
        .finally(function () {
          button.disabled = false;
        });
    }

    button.addEventListener('click', submit);
    [userInput, passInput].forEach(function (input) {
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') submit();
      });
    });

    card.appendChild(userField);
    card.appendChild(passField);
    card.appendChild(button);
    card.appendChild(errorNode);
    root.appendChild(card);

    userInput.focus();
  }

  /* ------------------------------------------------------------ загрузка */

  function loadAll() {
    return Promise.all([
      api('/admin/api/summary'),
      api('/admin/api/attempts?limit=300' + (state.testFilter ? '&test_id=' + encodeURIComponent(state.testFilter) : '')),
      api('/admin/api/log')
    ]).then(function (result) {
      state.summary = result[0];
      state.attempts = result[1].attempts || [];
      state.log = result[2].events || [];
    });
  }

  function boot() {
    return api('/admin/api/session')
      .then(function (session) {
        state.session = session;
        if (!session.authenticated) {
          renderLogin('');
          return;
        }
        return loadAll().then(render);
      })
      .catch(function (error) {
        if (error.status === 401) renderLogin('');
        else renderLogin(error.message);
      });
  }

  function logout() {
    api('/admin/api/logout', { method: 'POST', body: {} })
      .catch(function () {})
      .then(function () {
        state.session = null;
        state.summary = null;
        state.attempts = [];
        state.key = null;
        state.log = [];
        renderLogin('');
      });
  }

  /* ------------------------------------------------------------- отрисовка */

  function render() {
    clear(root);

    var top = el('div', 'admin-top');
    var titles = el('div');
    titles.appendChild(el('h1', 'admin-title', 'Панель администратора'));
    var sub = 'Вы вошли как ' + (state.session && state.session.username ? state.session.username : 'admin');
    if (state.session && state.session.expiresAt) {
      sub += ' · сессия до ' + fmtDate(new Date(state.session.expiresAt * 1000).toISOString());
    }
    titles.appendChild(el('p', 'admin-sub', sub));
    top.appendChild(titles);

    var actions = el('div', 'admin-bar');
    var refresh = el('button', 'mini-btn', 'Обновить');
    refresh.type = 'button';
    refresh.addEventListener('click', function () {
      loadAll().then(render).catch(function (error) {
        notice(error.message);
        render();
      });
    });
    var exportLink = el('a', 'mini-btn', 'Скачать CSV');
    exportLink.href = '/admin/api/export.csv';
    var out = el('button', 'mini-btn', 'Выйти');
    out.type = 'button';
    out.addEventListener('click', logout);
    actions.appendChild(refresh);
    actions.appendChild(exportLink);
    actions.appendChild(out);
    top.appendChild(actions);
    root.appendChild(top);

    if (state.session && state.session.passwordIsDefault) {
      root.appendChild(
        el(
          'div',
          'admin-warn',
          'Пароль администратора пока стартовый. Смените его командой: python admin_passwd.py'
        )
      );
    }

    if (state.notices.length) {
      var noticeBox = el('div', 'admin-warn');
      noticeBox.appendChild(el('span', null, state.notices.join(' ')));
      root.appendChild(noticeBox);
    }

    var totals = (state.summary && state.summary.totals) || {};
    var cards = el('div', 'admin-cards');
    [
      ['Попыток', totals.attempts || 0],
      ['Студентов', totals.students || 0],
      ['Зачёт', totals.passed || 0],
      ['Не зачёт', (totals.attempts || 0) - (totals.passed || 0)],
      ['С флагами', totals.flagged || 0]
    ].forEach(function (entry) {
      var card = el('div', 'admin-card');
      card.appendChild(el('div', 'admin-card__value', entry[1]));
      card.appendChild(el('div', 'admin-card__label', entry[0]));
      cards.appendChild(card);
    });
    root.appendChild(cards);

    var tabs = el('div', 'admin-tabs');
    [
      ['attempts', 'Попытки'],
      ['key', 'Ключ ответов'],
      ['log', 'Журнал входов']
    ].forEach(function (entry) {
      var tab = el('button', 'admin-tab' + (state.tab === entry[0] ? ' admin-tab--on' : ''), entry[1]);
      tab.type = 'button';
      tab.addEventListener('click', function () {
        state.tab = entry[0];
        if (entry[0] === 'key' && !state.key) {
          loadKey().then(render).catch(function (error) {
            notice(error.message);
            render();
          });
          return;
        }
        render();
      });
      tabs.appendChild(tab);
    });
    root.appendChild(tabs);

    if (state.tab === 'attempts') renderAttempts();
    if (state.tab === 'key') renderKey();
    if (state.tab === 'log') renderLog();

    if (state.detail) root.appendChild(renderDetail());

    if (timer) window.clearInterval(timer);
    if (state.tab === 'attempts' && !state.detail) {
      timer = window.setInterval(function () {
        if (document.hidden) return;
        loadAll().then(render).catch(function () {});
      }, REFRESH_MS);
    }
  }

  function renderAttempts() {
    var bar = el('div', 'admin-bar');
    var label = el('span', 'admin-note', 'Задание:');
    var select = el('select', 'admin-input');
    select.style.width = 'auto';
    select.appendChild(el('option', null, 'Все задания'));
    ((state.summary && state.summary.tests) || []).forEach(function (test) {
      var option = el('option', null, test.code + ' ' + test.title);
      option.value = test.id;
      if (state.testFilter === test.id) option.selected = true;
      select.appendChild(option);
    });
    select.addEventListener('change', function () {
      state.testFilter = select.value;
      loadAll().then(render).catch(function (error) {
        notice(error.message);
        render();
      });
    });
    bar.appendChild(label);
    bar.appendChild(select);
    bar.appendChild(el('span', 'admin-note', 'Показано: ' + state.attempts.length + ' · обновляется автоматически'));
    root.appendChild(bar);

    if (!state.attempts.length) {
      root.appendChild(el('div', 'admin-empty', 'Попыток пока нет.'));
      return;
    }

    var wrap = el('div', 'admin-scroll');
    var table = el('table', 'admin-table');
    var head = el('thead');
    var headRow = el('tr');
    ['№', 'Фамилия', 'Попытка', 'Баллы', 'Процент', 'Итог', 'Ответов', 'Время', 'Завершено', 'Флаги', ''].forEach(function (title) {
      headRow.appendChild(el('th', null, title));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = el('tbody');
    state.attempts.forEach(function (attempt) {
      var row = el('tr');
      row.appendChild(el('td', 'mono', attempt.id));
      row.appendChild(el('td', null, attempt.student));
      row.appendChild(el('td', null, '№' + attempt.attemptNumber));
      row.appendChild(el('td', null, attempt.score + ' / ' + attempt.total));
      row.appendChild(el('td', null, attempt.percent.toFixed(1).replace('.', ',') + ' %'));

      var verdict = el('td');
      verdict.appendChild(el('span', 'badge ' + (attempt.passed ? 'badge--pass' : 'badge--fail'), attempt.verdict));
      row.appendChild(verdict);

      row.appendChild(el('td', null, attempt.answered + ' / ' + attempt.total));
      row.appendChild(el('td', null, fmtDuration(attempt.durationSec)));
      row.appendChild(el('td', null, fmtDate(attempt.createdAt)));

      var flags = el('td');
      if (attempt.flags.length) {
        attempt.flags.forEach(function (flag) {
          flags.appendChild(el('span', 'badge badge--flag', flag));
        });
      } else {
        flags.appendChild(el('span', 'admin-note', '—'));
      }
      row.appendChild(flags);

      var open = el('td');
      var button = el('button', 'mini-btn', 'Ответы');
      button.type = 'button';
      button.addEventListener('click', function () {
        openDetail(attempt.id);
      });
      open.appendChild(button);
      row.appendChild(open);

      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    root.appendChild(wrap);
  }

  function openDetail(attemptId) {
    state.detail = { loading: true, id: attemptId, data: null };
    render();
    api('/admin/api/attempts/' + encodeURIComponent(attemptId))
      .then(function (data) {
        state.detail = { loading: false, id: attemptId, data: data };
        render();
      })
      .catch(function (error) {
        state.detail = { loading: false, id: attemptId, data: null, error: error.message };
        render();
      });
  }

  function renderDetail() {
    var modal = el('div', 'admin-modal');
    var inner = el('div', 'admin-modal__inner');

    var head = el('div', 'admin-modal__head');
    var info = el('div');
    if (state.detail.loading) {
      info.appendChild(el('h2', 'admin-title', 'Загружаю ответы…'));
    } else if (state.detail.error) {
      info.appendChild(el('h2', 'admin-title', state.detail.error));
    } else if (state.detail.data) {
      var attempt = state.detail.data.attempt;
      info.appendChild(el('h2', 'admin-title', 'Ответы: ' + attempt.student));
      info.appendChild(
        el(
          'p',
          'admin-sub',
          attempt.testTitle +
            ' · попытка №' +
            attempt.attemptNumber +
            ' · ' +
            attempt.score +
            ' из ' +
            attempt.total +
            ' (' +
            attempt.percent.toFixed(1).replace('.', ',') +
            ' %) · ' +
            attempt.verdict +
            ' · ' +
            fmtDate(attempt.createdAt) +
            ' · ' +
            fmtDuration(attempt.durationSec)
        )
      );
    }
    head.appendChild(info);

    var close = el('button', 'mini-btn', 'Закрыть');
    close.type = 'button';
    close.addEventListener('click', function () {
      state.detail = null;
      render();
    });
    head.appendChild(close);
    inner.appendChild(head);

    if (state.detail.data) {
      var attemptFlags = state.detail.data.attempt.flags;
      if (attemptFlags.length) {
        var flagBox = el('div', 'admin-bar');
        attemptFlags.forEach(function (flag) {
          flagBox.appendChild(el('span', 'badge badge--flag', flag));
        });
        inner.appendChild(flagBox);
      }

      var list = el('div', 'qa');
      state.detail.data.answers.forEach(function (item) {
        var row = el('div', 'qa-row ' + (item.ok ? 'qa-row--good' : 'qa-row--bad'));
        row.appendChild(el('div', 'qa-num', item.number));

        var cell = el('div');
        cell.appendChild(el('div', 'qa-q', item.question));

        var answerLine = el('div', 'qa-a');
        answerLine.appendChild(el('b', null, 'Ответ студента: '));
        answerLine.appendChild(
          el('span', item.ok ? 'ok-text' : 'bad-text', item.answered ? item.chosenLetter + ') ' + item.chosenText : 'нет ответа')
        );
        cell.appendChild(answerLine);

        var keyLine = el('div', 'qa-a');
        keyLine.appendChild(el('b', null, 'Верно: '));
        keyLine.appendChild(el('span', 'ok-text', item.correctLetter + ') ' + item.correctText));
        cell.appendChild(keyLine);

        row.appendChild(cell);
        list.appendChild(row);
      });
      inner.appendChild(list);
    }

    modal.appendChild(inner);
    modal.addEventListener('click', function (event) {
      if (event.target === modal) {
        state.detail = null;
        render();
      }
    });
    return modal;
  }

  function loadKey() {
    var path = '/admin/api/key' + (state.keyTest ? '?test_id=' + encodeURIComponent(state.keyTest) : '');
    return api(path).then(function (data) {
      state.key = data.tests || [];
    });
  }

  function renderKey() {
    if (!state.key) {
      var loading = el('div', 'admin-empty', 'Загружаю ключ ответов…');
      root.appendChild(loading);
      loadKey()
        .then(render)
        .catch(function (error) {
          notice(error.message);
          render();
        });
      return;
    }

    state.key.forEach(function (test) {
      var title = el('h2', 'admin-title', test.code + ' ' + test.title);
      title.style.marginTop = '22px';
      root.appendChild(title);
      root.appendChild(
        el('p', 'admin-sub', 'Всего ' + test.items.length + ' вопросов · порог ' + test.passingPercent + ' %')
      );

      var list = el('div', 'qa');
      list.style.marginTop = '14px';
      test.items.forEach(function (item) {
        var row = el('div', 'qa-row');
        row.appendChild(el('div', 'qa-num', item.number));
        var cell = el('div');
        cell.appendChild(el('div', 'qa-q', item.question));
        var line = el('div', 'qa-a');
        line.appendChild(el('b', null, 'Верно: '));
        line.appendChild(el('span', 'ok-text', item.correctLetter + ') ' + item.correctText));
        cell.appendChild(line);
        row.appendChild(cell);
        list.appendChild(row);
      });
      root.appendChild(list);
    });
  }

  function renderLog() {
    if (!state.log.length) {
      root.appendChild(el('div', 'admin-empty', 'Входов пока не было.'));
      return;
    }
    var wrap = el('div', 'admin-scroll');
    var table = el('table', 'admin-table');
    var head = el('thead');
    var headRow = el('tr');
    ['Когда', 'Логин', 'Итог', 'Примечание', 'Клиент'].forEach(function (title) {
      headRow.appendChild(el('th', null, title));
    });
    head.appendChild(headRow);
    table.appendChild(head);

    var body = el('tbody');
    state.log.forEach(function (event) {
      var row = el('tr');
      row.appendChild(el('td', null, fmtDate(event.createdAt)));
      row.appendChild(el('td', null, event.username));
      var verdict = el('td');
      verdict.appendChild(el('span', 'badge ' + (event.success ? 'badge--pass' : 'badge--fail'), event.success ? 'ок' : 'отказ'));
      row.appendChild(verdict);
      row.appendChild(el('td', null, event.note || '—'));
      row.appendChild(el('td', 'mono', event.clientHash));
      body.appendChild(row);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    root.appendChild(wrap);
  }

  boot();
})();
