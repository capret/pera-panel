document.querySelector('#login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  const error = document.querySelector('#login-error');
  button.disabled = true;
  error.textContent = '';
  try {
    const response = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}, body: JSON.stringify(Object.fromEntries(new FormData(event.target)))});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Sign in failed.');
    location.assign('/');
  } catch (failure) { error.textContent = failure.message; }
  finally { button.disabled = false; }
});
