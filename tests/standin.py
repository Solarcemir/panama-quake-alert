"""A stand-in for a WhatsApp channel's page, for tests. Nothing to do with WhatsApp.

Like WhatsApp Web it holds only part of the messages: the six newest when it opens or when a message is posted,
six older ones more each time its list is scrolled to the top, six newer ones each time it is scrolled to the end,
and never more than twelve, so that going up drops the newest. It draws a long message's first 768 characters and
then "Read more"; it edits through a right-click menu and a dialog; it posts what is entered in its box; Escape
with nothing open closes the channel's page, and the channel is opened again from the list of channels.
Switches for faults: window.deaf = true (the dialog's send button does nothing), window.garble = true (the dialog
does not hold what is pasted into it), window.slow = true (a posted message shows up only after 30 s); and
drift() leaves the list showing the oldest messages, as WhatsApp's was found on 2026-10-14.
"""
PAGE = """<!doctype html><meta charset="utf-8">
<div id="side"><div>SECRET CHAT NAME</div><button aria-label="Channels">Channels</button><span title="Stand-in">Stand-in</span></div>
<div id="main">
  <header><div>Stand-in</div><div>12 followers</div></header>
  <div id="list" style="height:260px; overflow-y:auto"></div>
  <footer><div contenteditable="true" style="min-height:2em; border:1px solid #888"></div></footer>
</div>
<script>
  const all = [], list = document.getElementById('list'), box = document.querySelector('footer div');
  const main = document.getElementById('main'), name = document.querySelector('#main header div');
  const CAP = 12;
  let from = 0, to = 0, busy = false, target = null;
  const cut = t => t.length > 768 ? t.slice(0, 768) + '\\u2026 Read more' : t;
  const draw = m => { const d = document.createElement('div'); d.dataset.id = m.id; d.style.minHeight = '90px';
    d.innerHTML = '<div class="t" style="white-space:pre-wrap"></div><span></span>'; d.firstChild.textContent = cut(m.text); d.lastChild.textContent = (m.edited ? 'Edited ' : '') + '9:00 AM'; return d; };
  const redraw = bottom => { busy = true; list.replaceChildren(...all.slice(from, to).map(draw)); if (bottom) list.scrollTop = list.scrollHeight; setTimeout(() => { busy = false; }, 30); };
  const newest = () => { to = all.length; from = Math.max(to - 6, 0); redraw(true); };
  let serial = 1000; window.addMessage = text => { all.push({id: 'true_1@newsletter_' + (serial++), text, edited: false}); newest(); };
  window.dropMessage = key => { for (let i = all.length - 1; i >= 0; i--) if (all[i].text.includes(key)) all.splice(i, 1); newest(); };
  window.drift = () => { from = 0; to = Math.min(6, all.length); redraw(false); list.scrollTop = 0; busy = true; setTimeout(() => { busy = false; }, 200); };
  window.textOf = key => { const hit = all.filter(m => m.text.includes(key)); return hit.length ? hit[hit.length - 1].text + (hit[hit.length - 1].edited ? ' [Edited]' : '') : null; };
  window.count = () => all.length;
  window.having = key => all.filter(m => m.text.includes(key)).length;
  window.atNewest = () => to === all.length && main.style.display !== 'none';
  window.edits = 0; window.reopened = 0;
  list.addEventListener('scroll', () => {
    if (busy) return;
    if (list.scrollTop < 5 && from > 0) {
      busy = true;
      setTimeout(() => { from = Math.max(from - 6, 0); to = Math.min(to, from + CAP); redraw(false); list.scrollTop = 120; }, 150);
    } else if (list.scrollTop + list.clientHeight >= list.scrollHeight - 5 && to < all.length) {
      busy = true;
      setTimeout(() => { to = Math.min(to + 6, all.length); from = Math.max(from, to - CAP); redraw(true); }, 150);
    }
  });
  box.addEventListener('paste', e => { e.preventDefault(); box.textContent = e.clipboardData.getData('text/plain'); });
  box.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); const t = box.textContent; box.textContent = ''; if (t) setTimeout(() => addMessage(t), window.slow ? 30000 : 150); } });
  const close = () => document.querySelectorAll('.pop').forEach(p => p.remove());
  document.addEventListener('contextmenu', e => {
    const row = e.target.closest('[data-id]'); if (!row) return;
    e.preventDefault(); close(); target = row.dataset.id;
    const pop = document.createElement('div'); pop.className = 'pop'; pop.setAttribute('role', 'application');
    pop.innerHTML = '<div role="menu"><button role="menuitem" aria-label="Copy">Copy</button><button role="menuitem" aria-label="Edit">Edit</button><button role="menuitem" aria-label="Delete">Delete</button></div>';
    document.body.append(pop);
  });
  document.addEventListener('click', e => {
    if (e.target.closest('[aria-label="Delete"]')) { window.deleted = true; close(); }
    if (e.target.closest('#side span[title="Stand-in"]')) { main.style.display = ''; name.textContent = 'Stand-in'; window.reopened++; newest(); }
    if (e.target.closest('[role="menuitem"][aria-label="Edit"]')) {
      close();
      const m = all.find(x => x.id === target);
      const pop = document.createElement('div'); pop.className = 'pop'; pop.setAttribute('role', 'dialog');
      pop.innerHTML = '<span>Edit update</span><div role="textbox" aria-label="Type an update" contenteditable="true" style="white-space:pre-wrap"></div><button aria-label="Send edited message">send</button>';
      document.body.append(pop);
      const edit = pop.querySelector('[role="textbox"]'); edit.textContent = m.text;
      edit.addEventListener('paste', ev => { ev.preventDefault(); edit.textContent = window.garble ? 'otra cosa' : ev.clipboardData.getData('text/plain'); });
      pop.querySelector('button').addEventListener('click', () => { if (window.deaf) return; m.text = edit.textContent; m.edited = true; window.edits++; close(); setTimeout(() => redraw(false), 200); });
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (document.querySelector('.pop')) close();
    else { main.style.display = 'none'; name.textContent = ''; }          // nothing open: the channel's page closes
  });
</script>"""
