const state = {
  token: localStorage.getItem("campus_token"),
  user: JSON.parse(localStorage.getItem("campus_user") || "null"),
  posts: [],
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => { const el = document.createElement("div"); el.textContent = value; return el.innerHTML; };

function toast(message) {
  const el = $("#toast"); el.textContent = message; el.style.display = "block";
  setTimeout(() => { el.style.display = "none"; }, 3500);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401 && state.token) clearSession();
  const data = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function saveSession(data) {
  state.token = data.access_token; state.user = data.user;
  localStorage.setItem("campus_token", state.token);
  localStorage.setItem("campus_user", JSON.stringify(state.user));
  showView(); loadAll();
}

function clearSession() {
  state.token = null; state.user = null;
  localStorage.removeItem("campus_token"); localStorage.removeItem("campus_user");
  showView();
}

function showView() {
  $("#auth-view").classList.toggle("hidden", !!state.token);
  $("#app-view").classList.toggle("hidden", !state.token);
  $("#logout").classList.toggle("hidden", !state.token);
  if (state.user) $("#welcome").textContent = `Welcome, ${state.user.display_name}`;
}

async function loadPosts() {
  const posts = await api("/api/messages");
  state.posts = posts;
  $("#posts").innerHTML = posts.map(post => `
    <article class="card post">
      <header><strong>${escapeHtml(post.author.display_name)}</strong><small>${new Date(post.created_at).toLocaleString()}</small></header>
      <p>${escapeHtml(post.body)}</p>
      <div class="actions">
        <button onclick="likePost(${post.id})">${post.liked_by_me ? "Liked" : "Like"} · ${post.like_count}</button>
        ${post.author.id === state.user.id ? `<button onclick="editPost(${post.id})">Edit</button>` : ""}
      </div>
    </article>`).join("") || '<div class="card">No messages yet. Write the first one!</div>';
}

async function loadFriends() {
  const friends = await api("/api/friends");
  $("#friends").innerHTML = friends.map(person => `<div class="person"><span>${escapeHtml(person.display_name)}<small>${escapeHtml(person.email)}</small></span></div>`).join("") || "No friends added yet.";
}

async function searchPeople(q = "") {
  const people = await api(`/api/users?q=${encodeURIComponent(q)}`);
  $("#people").innerHTML = people.map(person => `<div class="person"><span>${escapeHtml(person.display_name)}<small>${escapeHtml(person.email)}</small></span><button onclick="addFriend(${person.id})">Add</button></div>`).join("");
}

async function loadAll() {
  try { await Promise.all([loadPosts(), loadFriends(), searchPeople()]); } catch (error) { toast(error.message); }
}

const searchState = { q: "", sort: "relevance", tag: null, page: 1 };

async function runSearch() {
  const { q, sort, tag, page } = searchState;
  if (!q) { $("#search-meta").innerHTML = ""; $("#search-facets").innerHTML = ""; $("#search-results").innerHTML = ""; return; }
  const params = new URLSearchParams({ q, sort, page });
  if (tag) params.set("tag", tag);
  const data = await api(`/api/search?${params}`);
  const total = data.total ?? 0;
  const from = total ? (page - 1) * data.page_size + 1 : 0;
  const to = (page - 1) * data.page_size + data.results.length;
  $("#search-meta").innerHTML = `<p class="muted">${total.toLocaleString()} result${total === 1 ? "" : "s"}${tag ? ` in #${escapeHtml(tag)} <button class="link" onclick="setSearchTag(null)">clear</button>` : ""}${total ? ` · showing ${from}–${to}` : ""} · ${data.db_ms} ms</p>`;
  $("#search-facets").innerHTML = data.facets.map(f => `<button class="chip${f.tag === tag ? " active" : ""}" onclick="setSearchTag('${escapeHtml(f.tag)}')">#${escapeHtml(f.tag)} (${f.n})</button>`).join("");
  const pager = `<div class="actions pager">${page > 1 ? `<button onclick="searchPage(${page - 1})">Previous</button>` : ""}${to < total ? `<button onclick="searchPage(${page + 1})">Next</button>` : ""}</div>`;
  $("#search-results").innerHTML = data.results.map(hit => `
    <article class="card post">
      <header><strong>${escapeHtml(hit.author.display_name)}</strong><small>${new Date(hit.created_at).toLocaleString()} · ${hit.like_count} likes</small></header>
      <p>${escapeHtml(hit.body)}</p>
    </article>`).join("") + (data.results.length ? pager : (q ? '<div class="card">No matching posts.</div>' : ""));
}

window.setSearchTag = (tag) => { searchState.tag = tag; searchState.page = 1; runSearch().catch(e => toast(e.message)); };
window.searchPage = (page) => { searchState.page = page; runSearch().catch(e => toast(e.message)); };

window.addFriend = async (userId) => { try { await api("/api/friends", { method: "POST", body: JSON.stringify({ user_id: userId }) }); await loadFriends(); toast("Friend added"); } catch (e) { toast(e.message); } };
window.likePost = async (postId) => { try { await api(`/api/messages/${postId}/like`, { method: "POST" }); await loadPosts(); } catch (e) { toast(e.message); } };
window.editPost = async (postId) => { const post = state.posts.find(item => item.id === postId); const body = prompt("Edit your message", post?.body || ""); if (!body) return; try { await api(`/api/messages/${postId}`, { method: "PATCH", body: JSON.stringify({ body }) }); await loadPosts(); } catch (e) { toast(e.message); } };

$("#login-form").addEventListener("submit", async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); try { saveSession(await api("/api/auth/login", { method: "POST", body: JSON.stringify(values) })); event.target.reset(); } catch (e) { toast(e.message); } });
$("#register-form").addEventListener("submit", async event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); try { saveSession(await api("/api/auth/register", { method: "POST", body: JSON.stringify(values) })); event.target.reset(); } catch (e) { toast(e.message); } });
$("#post-form").addEventListener("submit", async event => { event.preventDefault(); const body = new FormData(event.target).get("body"); try { await api("/api/messages", { method: "POST", body: JSON.stringify({ body }) }); event.target.reset(); await loadPosts(); } catch (e) { toast(e.message); } });
$("#search-form").addEventListener("submit", event => { event.preventDefault(); searchPeople(new FormData(event.target).get("q")).catch(e => toast(e.message)); });
$("#post-search-form").addEventListener("submit", event => { event.preventDefault(); const values = Object.fromEntries(new FormData(event.target)); Object.assign(searchState, { q: values.q.trim(), sort: values.sort, tag: null, page: 1 }); runSearch().catch(e => toast(e.message)); });
$("#logout").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {} clearSession(); });

showView();
if (state.token) loadAll();
