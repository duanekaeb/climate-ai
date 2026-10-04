// Apply the theme before first paint: the saved choice, else the system setting.
try {
  var saved = localStorage.getItem('climate.theme')
  var dark = saved ? saved === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches
  document.documentElement.classList.toggle('dark', dark)
} catch (e) {}
