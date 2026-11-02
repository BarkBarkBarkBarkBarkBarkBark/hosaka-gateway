"use strict";
const login = document.getElementById("login");
const status = document.getElementById("status");
const apps = document.getElementById("apps");
const logout = document.getElementById("logout");

async function load() {
  const response = await fetch("/apps");
  if (response.status === 401) {
    login.hidden = false;
    logout.hidden = true;
    apps.replaceChildren();
    return;
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || "Cannot load apps.");
  login.hidden = true;
  logout.hidden = Boolean(body.dev_mode);
  const next = new URLSearchParams(location.search).get("next");
  if (next && body.apps.some((app) => app.available && next.startsWith(app.url))) {
    const target = new URL(next, location.origin);
    if (target.origin === location.origin && body.apps.some((app) => app.available && target.pathname.startsWith(app.url))) {
      location.replace(target.href);
      return;
    }
  }
  apps.replaceChildren();
  for (const app of body.apps) {
    const item = document.createElement(app.available ? "a" : "section");
    item.className = "app";
    if (app.available) item.href = app.url;
    const heading = document.createElement("h2");
    heading.textContent = app.name;
    const detail = document.createElement("p");
    detail.textContent = app.error || app.description || "Open application";
    item.append(heading, detail);
    apps.append(item);
  }
  status.textContent = body.apps.length ? (body.dev_mode ? "Local development mode active on loopback." : "") : "No approved web apps. Install an app, inspect it, and grant web access from your terminal.";
}

login.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = login.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/session", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.getElementById("password").value }),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Sign-in failed.");
    document.getElementById("password").value = "";
    status.textContent = "";
    await load();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
});
logout.addEventListener("click", async () => {
  try {
    const response = await fetch("/session", { method: "DELETE" });
    if (!response.ok) throw new Error("Sign-out failed.");
    await load();
  } catch (error) { status.textContent = error.message; }
});
load().catch((error) => { status.textContent = error.message; });
