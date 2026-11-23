const form = document.getElementById('login-form');
const err = document.getElementById('error');
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  err.textContent = '';
  const password = document.getElementById('password').value;
  const res = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  if (res.ok) {
    window.location.href = '/';
  } else {
    const body = await res.json().catch(() => ({}));
    err.textContent = body.detail || 'Login failed';
  }
});
