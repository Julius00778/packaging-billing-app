/* ============================================================================
   Type karke dhoondhne wala picker.

   Pehle party aur maal dono ek lambi <select> list se chunte the. Paanch naam
   the tab tak theek tha; pachees pe ungli ghumti reh jaati hai, aur phone pe
   toh wo list poori screen kha jaati hai. Ab naam type karo aur list chhant
   jaati hai.

   Do cheezein jaan-boojh ke:

   1. Asli value ek chhupe hue khaane me rehti hai, dikhne wale text me nahi.
      Do party ka naam milta-julta ho toh bhi galat id nahi jaati — aur agar
      aadmi kuch aisa type kar de jo list me hai hi nahi, toh chunav khaali
      reh jaata hai, kisi paas wale naam pe chup-chaap chipak nahi jaata.

   2. Keyboard se poora chalta hai (upar/neeche, Enter, Escape) aur screen
      reader ko bataata hai ki list khuli hai ya band, aur abhi kaunsa naam
      chuna hua hai. Bina iske ye picker un logon ke liye deewar ban jaata.

   Istemaal:
       Combo.attach(inputEl, hiddenEl, options, onPick)
       options = [{id: 3, label: "SARTHAK", note: "Aligarh"}, ...]
   ========================================================================= */
(function (global) {
  'use strict';

  var openBox = null;   // ek waqt me ek hi list khuli rahe

  function closeOpen() {
    if (openBox) { openBox.close(); }
  }

  document.addEventListener('click', function (e) {
    if (openBox && !openBox.root.contains(e.target)) { closeOpen(); }
  });

  function norm(s) {
    return String(s == null ? '' : s).toLowerCase().replace(/\s+/g, ' ').trim();
  }

  function attach(input, hidden, options, onPick) {
    var list = document.createElement('div');
    list.className = 'combo-list';
    list.setAttribute('role', 'listbox');
    list.hidden = true;

    var listId = input.id + '__list';
    list.id = listId;

    var root = input.parentNode;
    if (getComputedStyle(root).position === 'static') {
      root.style.position = 'relative';
    }
    root.appendChild(list);

    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', listId);
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('autocomplete', 'off');

    var shown = [];
    var active = -1;

    var api = {
      root: root,
      close: close,
      setOptions: function (next) { options = next || []; },
      options: function () { return options; }
    };

    function close() {
      list.hidden = true;
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      active = -1;
      if (openBox === api) { openBox = null; }
    }

    function pick(opt) {
      hidden.value = opt ? opt.id : '';
      input.value = opt ? opt.label : '';
      close();
      if (onPick) { onPick(opt); }
      // Form "badal gaya hai" jaan sake — programmatically set value se
      // change event apne aap nahi uthta.
      hidden.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function render(q) {
      var nq = norm(q);
      shown = options.filter(function (o) {
        return !nq || norm(o.label).indexOf(nq) !== -1 || norm(o.note).indexOf(nq) !== -1;
      }).slice(0, 50);

      list.innerHTML = '';
      if (!shown.length) {
        var none = document.createElement('div');
        none.className = 'combo-none';
        none.textContent = (global.COMBO_TXT && global.COMBO_TXT.none) || 'Kuch nahi mila';
        list.appendChild(none);
      }
      shown.forEach(function (o, i) {
        var row = document.createElement('div');
        row.className = 'combo-opt';
        row.id = listId + '__' + i;
        row.setAttribute('role', 'option');
        row.setAttribute('aria-selected', String(String(hidden.value) === String(o.id)));
        row.textContent = o.label;
        if (o.note) {
          var n = document.createElement('span');
          n.className = 'combo-note';
          n.textContent = o.note;
          row.appendChild(n);
        }
        // mousedown, click nahi — warna input ka blur pehle chal ke list
        // band kar deta hai aur click kisi ko milta hi nahi.
        row.addEventListener('mousedown', function (ev) {
          ev.preventDefault();
          pick(o);
        });
        list.appendChild(row);
      });
      setActive(shown.length ? 0 : -1);
    }

    function setActive(i) {
      active = i;
      var rows = list.querySelectorAll('.combo-opt');
      for (var k = 0; k < rows.length; k++) {
        rows[k].classList.toggle('is-active', k === i);
      }
      if (i >= 0 && rows[i]) {
        input.setAttribute('aria-activedescendant', rows[i].id);
        var r = rows[i];
        if (r.offsetTop < list.scrollTop) { list.scrollTop = r.offsetTop; }
        var bottom = r.offsetTop + r.offsetHeight;
        if (bottom > list.scrollTop + list.clientHeight) {
          list.scrollTop = bottom - list.clientHeight;
        }
      } else {
        input.removeAttribute('aria-activedescendant');
      }
    }

    function open() {
      closeOpen();
      openBox = api;
      list.hidden = false;
      input.setAttribute('aria-expanded', 'true');
    }

    input.addEventListener('focus', function () {
      render(input.value);
      open();
    });

    input.addEventListener('input', function () {
      // Type karte hi purana chunav chhod do — warna screen pe naya naam
      // dikhta hai aur andar purani id padi rehti hai.
      hidden.value = '';
      render(input.value);
      open();
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (list.hidden) { render(input.value); open(); return; }
        e.preventDefault();
        if (!shown.length) { return; }
        var next = active + (e.key === 'ArrowDown' ? 1 : -1);
        if (next < 0) { next = shown.length - 1; }
        if (next >= shown.length) { next = 0; }
        setActive(next);
      } else if (e.key === 'Enter') {
        if (!list.hidden && active >= 0 && shown[active]) {
          e.preventDefault();
          pick(shown[active]);
        }
      } else if (e.key === 'Escape') {
        if (!list.hidden) { e.stopPropagation(); close(); }
      }
    });

    input.addEventListener('blur', function () {
      // Jo likha hai wo bilkul kisi naam se milta ho toh use hi maan lo;
      // warna chunav khaali — aadha likha hua naam bill pe nahi jaana chahiye.
      setTimeout(function () {
        if (!hidden.value) {
          var exact = options.filter(function (o) { return norm(o.label) === norm(input.value); });
          if (exact.length === 1) {
            hidden.value = exact[0].id;
            input.value = exact[0].label;
            if (onPick) { onPick(exact[0]); }
          } else if (norm(input.value) === '') {
            input.value = '';
          }
        }
        close();
      }, 120);
    });

    return api;
  }

  global.Combo = { attach: attach, close: closeOpen };
})(window);
