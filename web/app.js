fetch("/health")
  .then((r) => r.json())
  .then((h) => {
    document.getElementById("health").textContent = JSON.stringify(h, null, 2);
  })
  .catch((e) => {
    document.getElementById("health").textContent = "health check failed: " + e;
  });
