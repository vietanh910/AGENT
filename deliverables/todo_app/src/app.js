'use strict';
const form = document.querySelector('#action-form');
const run = document.querySelector('#run');
const status = document.querySelector('#status');
const result = document.querySelector('#result');
const list = document.querySelector('#history-list');
const clear = document.querySelector('#clear');
const storageKey = 'action-lab-history-v1';
let history = [];
try {
  const saved = JSON.parse(localStorage.getItem(storageKey) || '[]');
  if (Array.isArray(saved)) history = saved.filter(item => item && typeof item.name === 'string' && ['success', 'failure'].includes(item.outcome) && Number.isFinite(item.time)).slice(0, 20);
} catch { document.querySelector('#storage-note').hidden = false; }
function save() {
  try { localStorage.setItem(storageKey, JSON.stringify(history)); }
  catch { document.querySelector('#storage-note').hidden = false; }
}
function render() {
  list.replaceChildren();
  document.querySelector('#empty').hidden = history.length > 0;
  clear.disabled = history.length === 0;
  for (const item of history) {
    const row = document.createElement('li');
    const details = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = item.name;
    const time = document.createElement('small');
    time.textContent = new Date(item.time).toLocaleString();
    const badge = document.createElement('span');
    badge.className = 'status' + (item.outcome === 'failure' ? ' failed' : '');
    badge.textContent = item.outcome === 'success' ? 'Passed' : 'Failed';
    details.append(name, time);
    row.append(details, badge);
    list.append(row);
  }
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (run.disabled) return;
  const nameInput = document.querySelector('#action-name');
  const name = nameInput.value.trim();
  if (!name) { nameInput.setCustomValidity('Enter an action name.'); nameInput.reportValidity(); return; }
  const outcome = document.querySelector('#outcome').value;
  run.disabled = true;
  status.textContent = 'Running…';
  result.className = 'result';
  result.textContent = 'Running your demo action…';
  form.setAttribute('aria-busy', 'true');
  await new Promise(resolve => setTimeout(resolve, 700));
  history.unshift({ name, outcome, time: Date.now() });
  history = history.slice(0, 20);
  save();
  render();
  status.textContent = outcome === 'success' ? 'Passed' : 'Failed';
  result.className = 'result ' + outcome;
  result.textContent = outcome === 'success' ? `“${name}” completed successfully.` : `“${name}” failed as requested by the demo outcome. Choose a successful outcome to try again.`;
  form.removeAttribute('aria-busy');
  run.disabled = false;
});
document.querySelector('#action-name').addEventListener('input', event => event.target.setCustomValidity(''));
clear.addEventListener('click', () => { history = []; save(); render(); });
render();
