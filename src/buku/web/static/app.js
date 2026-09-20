/* buku — small enhancement layer.
   HTMX handles search, admin review actions, and logout; this file only
   bridges the two JSON forms (login, password change) so they can post to
   the existing JSON API endpoints without the htmx json-enc extension. */

"use strict";

async function postJson(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* non-JSON body (e.g. 204); ignore */
  }
  return { ok: response.ok, status: response.status, payload };
}

function bindJsonForm(formId, url, onOk, onError) {
  const form = document.getElementById(formId);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const { ok, payload } = await postJson(url, data);
    if (ok) {
      onOk(payload);
    } else {
      onError(payload.detail || "Something went wrong. Please try again.");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const loginForm = document.getElementById("login-form");
  if (loginForm) {
    const errorBox = document.getElementById("login-error");
    bindJsonForm(
      "login-form",
      "/api/v1/auth/login",
      () => {
        const next = new URLSearchParams(window.location.search).get("next") || "/";
        window.location.assign(next);
      },
      (message) => {
        if (errorBox) {
          errorBox.textContent = message;
          errorBox.hidden = false;
        }
      },
    );
  }

  const passwordForm = document.getElementById("password-form");
  if (passwordForm) {
    const messageBox = document.getElementById("pw-message");
    bindJsonForm(
      "password-form",
      "/change-password",
      () => {
        if (messageBox) {
          messageBox.textContent = "Password changed.";
          messageBox.className = "message ok";
          messageBox.hidden = false;
        }
        passwordForm.reset();
      },
      (message) => {
        if (messageBox) {
          messageBox.textContent = message;
          messageBox.className = "message error";
          messageBox.hidden = false;
        }
      },
    );
  }
});