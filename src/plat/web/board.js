"use strict";
let token = "",
  data = [],
  cursor = 0,
  member = null,
  members = [],
  saved = [],
  pending = null,
  sessionKey = "",
  generation = 0,
  refreshing = false;
function dirtyRoom() {
  return Boolean(el("room").querySelector("form[data-dirty]"));
}
let view = "ticket",
  roomId = null,
  roomTab = "overview",
  roomData = null,
  connected = false;
const el = (id) => document.getElementById(id);
const labels = {
  attention: "Needs attention",
  mine: "My work",
  ticket: "Tickets",
  product: "Products",
  engineer: "Engineers",
  service: "Services",
  repo: "Repositories",
  review: "Reviews",
  delivery: "Delivery",
};
function node(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function button(text, fn, cls) {
  const b = node("button", text, cls);
  b.type = "button";
  b.onclick = fn;
  return b;
}
function field(label, input) {
  const l = node("label", label);
  l.append(input);
  return l;
}
function select(options, value) {
  const s = node("select");
  for (const [v, t] of options) {
    const o = node("option", t);
    o.value = v;
    s.append(o);
  }
  if (value !== undefined) s.value = value;
  return s;
}
function textInput(value = "", max = 80) {
  const i = node("input");
  i.value = value;
  i.maxLength = max;
  i.required = true;
  return i;
}
function table(headers, rows) {
  const wrap = node("div", undefined, "table-wrap"),
    t = node("table"),
    head = node("thead"),
    hr = node("tr"),
    body = node("tbody");
  headers.forEach((h) => hr.append(node("th", h)));
  head.append(hr);
  for (const cells of rows) {
    const r = node("tr");
    for (const c of cells) {
      const td = node("td");
      td.append(
        c instanceof Node ? c : document.createTextNode(String(c ?? "—")),
      );
      r.append(td);
    }
    body.append(r);
  }
  t.append(head, body);
  wrap.append(t);
  return wrap;
}
function money(n) {
  return "$" + Number(n || 0).toFixed(2);
}
function when(d) {
  return d ? new Date(d).toLocaleString() : "—";
}
function owner(p, l) {
  return l.owner || p.owner || "Unassigned";
}
function attention(p, l) {
  return Boolean(l?.needs_attention);
}
function status(p) {
  if (p.status === "planned")
    return p.stage === "development" ? "Ready to start" : "Plan pending";
  if (p.status === "running") return "Execution in progress";
  if (p.status === "closed") return "Shipped";
  if (p.status === "delivered") return "Local development complete";
  return p.status;
}
function settings() {
  return {
    view,
    sort: el("sort").value,
    product: el("product").value,
    query: el("filter").value,
    closed: el("closed").checked,
  };
}
function urlState() {
  const s = settings(),
    params = new URLSearchParams();
  for (const [k, v] of Object.entries(s)) if (v) params.set(k, String(v));
  if (roomId) params.set("room", roomId);
  history.replaceState(null, "", "#" + params);
}
function applySettings(s) {
  view = labels[s.view] ? s.view : "ticket";
  el("sort").value = ["attention", "ticket", "engineer", "repo"].includes(
    s.sort,
  )
    ? s.sort
    : "attention";
  el("filter").value = s.query || "";
  el("closed").checked = s.closed === true || s.closed === "true";
  if (
    s.product &&
    !Array.from(el("product").options).some((o) => o.value === s.product)
  )
    el("product").append(new Option(s.product, s.product));
  el("product").value = s.product || "";
}
async function api(path, body) {
  const session = generation,
    t = token;
  const r = await fetch("/api/" + path, {
    method: body ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + t,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json();
  if (session !== generation) throw Error("Session changed");
  if (!r.ok) {
    const e = Error(j.error || "Request failed");
    e.status = r.status;
    throw e;
  }
  return j;
}
function storePending() {
  try {
    if (pending) sessionStorage.setItem(sessionKey, JSON.stringify(pending));
    else sessionStorage.removeItem(sessionKey);
  } catch {
    el("error").textContent =
      "Browser storage unavailable; keep this tab open until the pending command is resolved.";
  }
  el("retry").hidden = !pending;
}
async function send(path, command) {
  if (!connected) {
    el("error").textContent = "Reconnect before issuing commands.";
    return false;
  }
  if (pending) {
    el("error").textContent =
      "Resolve the pending command before creating another.";
    return false;
  }
  pending = { path, body: { ...command, key: crypto.randomUUID() } };
  storePending();
  return retry();
}
async function retry() {
  if (!pending) return false;
  try {
    await api(pending.path, pending.body);
    for (const form of document.querySelectorAll("#room-content form"))
      form.dispatchEvent(
        new CustomEvent("commandconfirmed", { detail: pending }),
      );
    pending = null;
    storePending();
    await refresh(false);
    return true;
  } catch (e) {
    el("error").textContent = e.message;
    if (e.status && e.status < 500) {
      pending = null;
      storePending();
    }
    return false;
  }
}
function render() {
  el("view-title").textContent = labels[view];
  rootNav();
  el("board").replaceChildren();
  const q = el("filter").value.trim().toLowerCase(),
    product = el("product").value;
  let rows = [];
  for (const p of data) {
    if (!el("closed").checked && p.status === "closed") continue;
    for (const l of p.lots.length
      ? p.lots
      : [
          {
            id: null,
            key: "Scope not registered",
            repo: "—",
            state: "PENDING",
            classification: { products: [], services: [] },
          },
        ]) {
      const products = l.classification.products.length
          ? l.classification.products
          : ["Unclassified"],
        services = l.classification.services.length
          ? l.classification.services
          : ["Unclassified"];
      if (product && !products.includes(product)) continue;
      if (
        q &&
        ![p.anchor, p.title, owner(p, l), l.repo, ...products, ...services]
          .join(" ")
          .toLowerCase()
          .includes(q)
      )
        continue;
      const mine =
        owner(p, l) === member?.name ||
        p.items.some(
          (i) =>
            i.state === "open" &&
            i.owner === member?.name &&
            (!i.lot_id || i.lot_id === l.id),
        );
      if (view === "mine" && !mine) continue;
      if (view === "attention" && !attention(p, l)) continue;
      if (
        view === "review" &&
        !p.items.some(
          (i) =>
            i.kind === "review" &&
            i.state === "open" &&
            (!i.lot_id || i.lot_id === l.id),
        )
      )
        continue;
      if (
        view === "delivery" &&
        !["delivered", "closed"].includes(p.status) &&
        ![
          "integration",
          "recording",
          "deployment",
          "deploying",
          "validation",
          "ready",
        ].includes(p.stage)
      )
        continue;
      rows.push({ p, l, products, services });
    }
  }
  const sort = el("sort").value;
  rows.sort((a, b) =>
    sort === "attention"
      ? Number(Boolean(attention(b.p, b.l))) -
        Number(Boolean(attention(a.p, a.l)))
      : String(
          sort === "ticket"
            ? a.p.anchor
            : sort === "engineer"
              ? owner(a.p, a.l)
              : a.l.repo,
        ).localeCompare(
          String(
            sort === "ticket"
              ? b.p.anchor
              : sort === "engineer"
                ? owner(b.p, b.l)
                : b.l.repo,
          ),
        ),
  );
  const groups = new Map();
  for (const r of rows) {
    const keys =
      view === "product"
        ? r.products
        : view === "service"
          ? r.services
          : view === "engineer"
            ? [owner(r.p, r.l)]
            : view === "repo"
              ? [r.l.repository || r.l.repo]
              : [r.p.id];
    for (const k of keys) {
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
  }
  for (const [key, items] of groups) {
    const section = node("section", undefined, "card"),
      p = items[0].p;
    section.append(
      node(
        "h3",
        key === p.id ? p.anchor + " · " + (p.title || "Untitled") : key,
      ),
    );
    section.append(
      node(
        "p",
        new Set(items.map((r) => r.p.id)).size +
          " ticket(s) · " +
          items.filter((r) => r.l.id).length +
          " lot(s)",
        "meta",
      ),
    );
    section.append(
      table(
        ["Ticket / lot", "Responsible engineer", "State", "Next step"],
        items.map(({ p, l }) => [
          button(p.anchor + " / " + l.key, () => openRoom(p.id), "secondary"),
          owner(p, l),
          l.executor?.reconciliation_required
            ? "Interrupted · reconcile"
            : l.state,
          l.next_step || "Register the planned lots",
        ]),
      ),
    );
    el("board").append(section);
  }
  el("counts").textContent =
    new Set(rows.map((r) => r.p.id)).size +
    " unique ticket(s) · " +
    rows.filter((r) => r.l.id).length +
    " unique lot(s)";
  if (!rows.length)
    el("board").append(
      node(
        "p",
        "No work matches this view. Adjust the filters or register a plan.",
        "empty",
      ),
    );
}
function rootNav() {
  document
    .querySelectorAll("[data-view]")
    .forEach((b) =>
      b.setAttribute("aria-pressed", String(view === b.dataset.view)),
    );
}
function savedViews() {
  el("saved").replaceChildren();
  for (const v of saved) {
    const row = node("div", undefined, "saved-row");
    row.append(
      button(
        v.name + (v.shared ? " · team" : ""),
        () => {
          if (dirtyRoom() && !confirm("Discard the unsent draft?")) return;
          applySettings(v.settings);
          roomId = null;
          showList();
          urlState();
        },
        "secondary",
      ),
    );
    if (v.owner === member.name || (v.shared && member.role === "admin")) {
      const remove = button(
        "×",
        () => send("workspace", { action: "delete_view", id: v.id }),
        "secondary",
      );
      remove.setAttribute("aria-label", "Delete saved view " + v.name);
      row.append(remove);
    }
    el("saved").append(row);
  }
  el("view-shared").disabled = member?.role === "viewer";
}
function showList() {
  el("room-content").replaceChildren();
  el("list").hidden = false;
  el("room").hidden = true;
  render();
}
async function openRoom(id) {
  roomId = id;
  roomData = null;
  el("list").hidden = true;
  el("room").hidden = false;
  el("room-content").textContent = "Loading ticket…";
  urlState();
  const g = generation;
  try {
    const details = await api("room?id=" + encodeURIComponent(id));
    if (roomId !== id || g !== generation) return;
    roomData = details;
    renderRoom();
  } catch (e) {
    if (g === generation) el("error").textContent = e.message;
  }
}
function composer(p, existing) {
  const details = node("details");
  details.append(
    node(
      "summary",
      existing ? "Respond / reassign" : "Assign, ask, review or leave a note",
    ),
  );
  const form = node("form", undefined, "composer");
  form.addEventListener("input", () => (form.dataset.dirty = "true"));
  const actions = existing
    ? [
        [
          existing.kind === "question"
            ? "answer"
            : existing.kind === "review"
              ? "review_result"
              : "reassign",
          existing.kind === "question"
            ? "Answer question"
            : existing.kind === "review"
              ? "Record review"
              : "Reassign",
        ],
      ]
    : [
        ["assign", "Assign work"],
        ["question", "Ask a question"],
        ["review", "Request review"],
        ["note", "Leave a note"],
      ];
  const action = select(actions),
    scope = select(
      [
        ["", "Entire ticket"],
        ...p.lots.map((l) => [l.id, l.key + " · " + l.repo]),
      ],
      existing?.lot_id || "",
    );
  const target = select(
    members.map((m) => [m.name, m.name]),
    existing?.owner || member.name,
  );
  const text = node("textarea");
  text.required = true;
  text.maxLength = 12000;
  text.rows = 5;
  text.placeholder =
    "Task, expected outcome, relevant context, links, and what done looks like.";
  if (existing?.kind === "assign") text.value = existing.body.text;
  const brief = field(
    existing ? "Response / assignment brief" : "Assignment brief / message",
    text,
  );
  brief.className = "wide";
  const outcome = select([
    ["approved", "Approved"],
    ["changes_requested", "Changes requested"],
  ]);
  form.append(
    field("Action", action),
    field("Scope", scope),
    field("Engineer", target),
  );
  if (existing) {
    action.disabled = true;
    scope.disabled = true;
    if (existing.kind !== "assign") target.disabled = true;
    if (existing.kind === "review")
      form.append(field("Review outcome", outcome));
  }
  form.append(brief);
  const controls = node("div", undefined, "button-group wide"),
    expand = button("Expand editor", () => {
      const expanded = text.classList.toggle("expanded");
      expand.textContent = expanded ? "Collapse editor" : "Expand editor";
      expand.setAttribute("aria-expanded", String(expanded));
      text.style.height = "auto";
      text.style.height = text.scrollHeight + "px";
    });
  expand.setAttribute("aria-expanded", "false");
  text.oninput = () => {
    text.style.height = "auto";
    text.style.height = text.scrollHeight + "px";
  };
  const submit = node(
    "button",
    existing ? "Record response" : "Record assignment",
  );
  submit.type = "submit";
  action.onchange = () => {
    submit.textContent =
      action.value === "assign"
        ? "Record assignment"
        : "Record " + action.value;
  };
  controls.append(expand, submit);
  form.append(controls);
  form.onsubmit = async (e) => {
    e.preventDefault();
    if (!text.value.trim()) return;
    submit.disabled = true;
    let req = { plat: p.id, action: action.value, owner: target.value };
    if (existing)
      req = {
        ...req,
        item: existing.id,
        version: existing.version,
        note: text.value,
        outcome: outcome.value,
      };
    else
      req = {
        ...req,
        lot: scope.value || undefined,
        body: { text: text.value },
      };
    const ok = await send("commands", req);
    if (ok) {
      delete form.dataset.dirty;
      await refresh(false);
    } else submit.disabled = false;
  };
  details.append(form);
  return details;
}
function classification(p, l) {
  const details = node("details");
  details.append(node("summary", "Organize " + l.repo));
  const form = node("form", undefined, "composer"),
    products = textInput(l.classification.products.join(", "), 1600),
    services = textInput(l.classification.services.join(", "), 1600);
  form.addEventListener("input", () => (form.dataset.dirty = "true"));
  products.required = false;
  services.required = false;
  form.append(
    field("Products (comma separated)", products),
    field("Services (comma separated)", services),
  );
  const submit = node("button", "Save classification");
  form.append(submit);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const ok = await send("workspace", {
      action: "classify",
      repository: l.repository,
      version: l.classification.version,
      products: products.value
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
      services: services.value
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
    });
    if (ok) {
      delete form.dataset.dirty;
      await refresh(false);
    }
  };
  details.append(form);
  return details;
}
function renderRoom() {
  const p = data.find((p) => p.id === roomId);
  if (!p) {
    el("room-content").textContent = "This ticket is not in the current board.";
    return;
  }
  const r = roomData;
  if (!r) return;
  const root = el("room-content");
  root.replaceChildren();
  root.append(
    node("p", p.anchor, "eyebrow"),
    node("h2", p.title || p.anchor),
    node("span", status(p), "tag"),
    node("span", "Workflow: " + p.stage + " · v" + p.version, "tag"),
    node(
      "p",
      "Owner: " +
        (p.owner || "Unassigned") +
        " · " +
        money(p.spent) +
        (p.estimated ? " estimated" : " recorded") +
        " / " +
        (p.budget ? money(p.budget) + " approximate budget" : "unbounded"),
      "meta",
    ),
  );
  const tabs = node("div", undefined, "button-group tabs");
  for (const t of [
    "overview",
    "delivery",
    "evidence",
    "sessions",
    "decisions",
    "coordinate",
    "execution",
  ]) {
    const b = button(
      t[0].toUpperCase() + t.slice(1),
      () => {
        if (dirtyRoom() && !confirm("Discard the unsent draft?")) return;
        roomTab = t;
        renderRoom();
      },
      "secondary",
    );
    b.setAttribute("aria-pressed", String(roomTab === t));
    tabs.append(b);
  }
  root.append(tabs);
  if (roomTab === "overview") {
    root.append(
      table(
        ["Lot / repository", "Owner", "Phases", "State / attempt", "Executor"],
        p.lots.map((l) => [
          l.key + " · " + l.repo,
          owner(p, l) + (l.owner ? "" : " (ticket owner)"),
          l.phases.join(" → ") || "Legacy configuration",
          l.state + " / " + l.attempt,
          l.executor
            ? l.executor.reconciliation_required
              ? "Needs reconciliation"
              : l.executor.installation
            : "Not claimed",
        ]),
      ),
    );
    root.append(node("h3", "Delivery readiness"));
    root.append(
      node(
        "p",
        p.readiness
          ? p.readiness.ready
            ? "Evidence complete"
            : p.readiness.missing.join(" · ")
          : "Legacy local-development run; no full lifecycle enabled.",
      ),
    );
    if (member.role !== "viewer")
      p.lots.forEach((l) => root.append(classification(p, l)));
    root.append(
      node(
        "p",
        "Use the Execution tab to request Start, Pause, or Resume on the owning machine. Deployment remains a local CLI operation.",
        "meta",
      ),
    );
  } else if (roomTab === "delivery") {
    root.append(deliveryPanel(p, r.delivery), executionControls(p, r.execution, true));
  } else if (roomTab === "execution") {
    root.append(executionControls(p, r.execution));
  } else if (roomTab === "evidence") {
    root.append(
      node("h3", "Acceptance criteria"),
      table(
        ["Criterion", "Lot", "Requirement", "Verified attempt"],
        r.criteria.map((c) => [
          c.id,
          c.lot || "Run",
          c.statement,
          c.verified_attempt || "Not verified",
        ]),
      ),
    );
    root.append(node("h3", "Findings"));
    for (const f of r.findings) {
      const item = node("div", undefined, "item");
      item.append(
        node(
          "strong",
          f.severity + " · " + f.file + (f.line ? ":" + f.line : ""),
        ),
        node("p", f.claim),
        node("p", "Required fix: " + f.required_fix),
        node(
          "p",
          f.dismissed
            ? "Dismissed: " + f.dismissed
            : f.resolved
              ? "Resolved in attempt " + f.resolved
              : "Unresolved",
          "meta",
        ),
      );
      root.append(item);
    }
    if (!r.findings.length)
      root.append(node("p", "No recorded findings.", "meta"));
    root.append(
      node("h3", "Accepted stages"),
      table(
        ["Stage", "Version", "Accepted by", "When"],
        r.stages.map((s) => [s.stage, s.version, s.actor, when(s.created)]),
      ),
    );
    root.append(
      node("h3", "Artifact manifest"),
      node(
        "p",
        "Hashes identify recorded evidence. Raw narratives, prompts and transcripts are not shared by this endpoint.",
        "meta",
      ),
      table(
        ["Artifact", "Phase", "Attempt", "SHA-256"],
        r.artifacts.map((a) => [a.name, a.kind, a.attempt, a.sha256]),
      ),
    );
  } else if (roomTab === "sessions") {
    root.append(
      table(
        ["Lot", "Provider", "Owner", "State", "Last observed"],
        p.sessions.map((s) => [
          p.lots.find((l) => l.id === s.lot_id)?.key || "—",
          s.provider,
          s.owner,
          s.state + (s.stale ? " · stale observation" : ""),
          when(s.activity),
        ]),
      ),
    );
    root.append(
      node("h3", "Phase attempts"),
      table(
        ["Lot / phase", "Provider / model", "Outcome", "Finished", "Cost"],
        r.attempts.map((a) => [
          a.lot + " / " + a.phase,
          a.provider + " / " + a.model,
          a.finished ? "Exit " + a.exit_code : "No completion recorded",
          when(a.finished),
          money(a.cost) + (a.estimated ? " estimated" : ""),
        ]),
      ),
    );
  } else if (roomTab === "decisions") {
    root.append(
      node(
        "p",
        "Latest 200 decisions. Agent statements are self-reported; system checks are observed evidence.",
        "meta",
      ),
    );
    for (const d of r.decisions) {
      const i = node("div", undefined, "item");
      i.append(
        node("strong", d.decision),
        node("p", d.rationale),
        node("p", d.actor + " · " + d.by + " · " + when(d.at), "meta"),
      );
      root.append(i);
    }
  } else {
    for (const i of p.items) {
      const item = node("div", undefined, "item");
      item.append(
        node(
          "strong",
          i.kind +
            " → " +
            i.owner +
            " · " +
            i.lot +
            " · " +
            (i.stale ? "Stale review" : i.state),
        ),
        node("p", i.body.text),
        node("p", i.body.answer || ""),
      );
      if (
        member.role !== "viewer" &&
        p.status !== "closed" &&
        i.state === "open" &&
        (i.owner === member.name || member.role === "admin") &&
        ["assign", "question", "review"].includes(i.kind)
      )
        item.append(composer(p, i));
      root.append(item);
    }
    if (member.role !== "viewer" && p.status !== "closed")
      root.append(composer(p));
    else
      root.append(
        node(
          "p",
          p.status === "closed"
            ? "Shipped work is immutable."
            : "Viewer access is read-only.",
          "meta",
        ),
      );
  }
}
async function refresh(force = false) {
  if (!token || refreshing) return;
  refreshing = true;
  const g = generation;
  try {
    const b = await api("board");
    if (g !== generation) return;
    data = b.plats;
    member = b.member;
    members = b.members;
    saved = b.views;
    connected = true;
    el("login").hidden = true;
    el("workspace").hidden = false;
    el("identity").textContent = member.name + " · " + member.role;
    const oldProduct = el("product").value;
    el("product").replaceChildren(new Option("All products", ""));
    const products = new Set([
      "Unclassified",
      ...data.flatMap((p) => p.lots.flatMap((l) => l.classification.products)),
    ]);
    if (oldProduct) products.add(oldProduct);
    Array.from(products)
      .sort()
      .forEach((p) => el("product").append(new Option(p, p)));
    el("product").value = oldProduct;
    savedViews();
    render();
    el("connection").textContent = "Updated " + when(b.observed_at);
    el("error").textContent = "";
    if (roomId) {
      const editing = dirtyRoom();
      if (force || !editing) {
        roomData = await api("room?id=" + encodeURIComponent(roomId));
        if (g !== generation) return;
        renderRoom();
        el("list").hidden = true;
        el("room").hidden = false;
      }
    }
    const ev = await api("events?after=" + cursor);
    if (g !== generation) return;
    cursor = ev.cursor;
    for (const e of ev.events)
      el("events").prepend(node("li", e.message + " · " + when(e.at)));
    while (el("events").children.length > 100) el("events").lastChild.remove();
  } catch (e) {
    if (g === generation) {
      connected = false;
      el("error").textContent = e.message;
      el("connection").textContent = "Disconnected · previous observation only";
    }
  } finally {
    if (g === generation) refreshing = false;
  }
}
for (const [key, title] of Object.entries(labels)) {
  const b = button(
    title,
    () => {
      if (dirtyRoom() && !confirm("Discard the unsent draft?")) return;
      view = key;
      roomId = null;
      showList();
      urlState();
    },
    "secondary",
  );
  b.dataset.view = key;
  el("navigation").append(b);
}
applySettings(Object.fromEntries(new URLSearchParams(location.hash.slice(1))));
roomId = new URLSearchParams(location.hash.slice(1)).get("room");
el("login").onsubmit = async (e) => {
  e.preventDefault();
  generation++;
  refreshing = false;
  token = el("token").value;
  el("token").value = "";
  data = [];
  cursor = 0;
  el("events").replaceChildren();
  sessionKey =
    "plat-command-" +
    Array.from(
      new Uint8Array(
        await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token)),
      ),
    )
      .map((x) => x.toString(16).padStart(2, "0"))
      .join("");
  try {
    pending = JSON.parse(sessionStorage.getItem(sessionKey) || "null");
  } catch {
    pending = null;
  }
  el("retry").hidden = !pending;
  await refresh(true);
};
el("refresh").onclick = () => {
  if (dirtyRoom()) {
    el("error").textContent =
      "Your unsent draft is preserved. Submit it or leave the ticket before refreshing its forms.";
    return;
  }
  refresh(true);
};
for (const name of ["filter", "product", "sort", "closed"])
  el(name).addEventListener(name === "filter" ? "input" : "change", () => {
    render();
    urlState();
  });
el("back").onclick = () => {
  if (dirtyRoom() && !confirm("Discard the unsent draft?")) return;
  roomId = null;
  showList();
  urlState();
};
el("disconnect").onclick = () => {
  if (dirtyRoom() && !confirm("Discard the unsent draft and disconnect?"))
    return;
  generation++;
  refreshing = false;
  connected = false;
  token = "";
  data = [];
  members = [];
  saved = [];
  member = null;
  roomData = null;
  pending = null;
  el("workspace").hidden = true;
  el("room-content").replaceChildren();
  el("board").replaceChildren();
  el("events").replaceChildren();
  el("retry").hidden = true;
  el("login").hidden = false;
  el("connection").textContent = "Not connected";
};
el("retry").onclick = retry;
el("save-view").onsubmit = async (e) => {
  e.preventDefault();
  await send("workspace", {
    action: "save_view",
    name: el("view-name").value,
    shared: el("view-shared").checked,
    settings: settings(),
  });
};
window.addEventListener("beforeunload", (e) => {
  if (dirtyRoom()) {
    e.preventDefault();
    e.returnValue = "";
  }
});
setInterval(() => refresh(false), 15000);

function executionControls(p, execution, deliveryOnly = false) {
  const root = node("section");
  const machines = execution.executors.filter((e) => e.matches);
  root.append(node("h3", "Owning machine"));
  root.append(
    node(
      "p",
      machines.length
        ? machines
            .map(
              (e) =>
                `${e.name} · ${e.online ? "Online" : "Offline"} · last seen ${when(e.seen)}`,
            )
            .join("; ")
        : "No registered executor matches all lots. Register and connect the owning machine locally.",
    ),
  );
  root.append(
    node(
      "p",
      deliveryOnly ? "Refresh requests use the owning machine’s provider login. Acknowledged means received; completed means status was collected, not that CI passed. No agents or workflow changes." : "Requests are authenticated and recorded. Acknowledged means the machine received the request; completed means the control finished, not that the ticket is delivered. Pause allows in-flight agents to finish.",
      "meta",
    ),
  );
  if (member.role !== "viewer" && machines.length) {
    const form = node("form", undefined, "composer"),
      buttons = node("div", undefined, "button-group");
    const machine = select(
      machines.map((e) => [
        e.id,
        e.name + (e.online ? " · online" : " · offline"),
      ]),
    );
    const note = node("textarea");
    note.rows = 3;
    note.maxLength = 4000;
    note.required = true;
    note.placeholder = "Reason and context for this execution request";
    note.oninput = () => {
      form.dataset.dirty = "true";
      note.style.height = "auto";
      note.style.height = note.scrollHeight + "px";
    };
    const preview = node(
      "p",
      "Choose a control to review its effect before submitting.",
      "meta wide",
    );
    let action;
    const submit = node("button", "Review a control first");
    submit.type = "submit";
    submit.disabled = true;
    for (const a of deliveryOnly ? ["refresh_delivery"] : ["start", "pause", "resume"]) {
      const b = button(
        a === "refresh_delivery" ? "Refresh PR / CI status" : a[0].toUpperCase() + a.slice(1),
        () => {
          action = a;
          form.dataset.dirty = "true";
          preview.textContent = `${a.toUpperCase()} ${p.anchor} on ${machines.find((e) => e.id === machine.value)?.name}. Workflow v${p.version}; ${execution.scope.lots.length} lot(s) (${execution.scope.lots.map((l) => l.key + ": " + l.phases.join(" → ")).join("; ")}); ${p.budget ? money(execution.scope.budget) + " approximate budget" : "unbounded budget"}. ${a === "pause" ? "Current agents may finish; no new phase should start." : "Launch the configured phases serially using the machine's logged-in provider accounts."}`;
          if (a === "refresh_delivery") preview.textContent = `Refresh ${execution.delivery_targets.length} linked PR/MR(s) on ${machines.find(e => e.id === machine.value)?.name}: ${execution.delivery_targets.map(t => t.lot + " · " + t.url).join("; ")}. Read provider status using that laptop's login. No agents, external writes, or workflow changes.`;
          submit.textContent = a === "refresh_delivery" ? "Request status refresh" : "Request " + a;
          submit.disabled = false;
        },
        "secondary",
      );
      b.disabled =
        !connected ||
        !machines.some((e) => e.online) ||
        !{
          start: ["planned"],
          pause: ["planned", "running"],
          resume: ["paused"],
          refresh_delivery: execution.delivery_targets.length ? ["planned", "running", "paused", "delivered"] : [],
        }[a].includes(p.status);
      buttons.append(b);
    }
    machine.onchange = () => {
      action = undefined;
      submit.disabled = true;
      preview.textContent = "Machine changed; choose a control again.";
    };
    const reason = field("Reason", note);
    reason.className = "wide";
    const controls = node("div", undefined, "button-group wide");
    const expand = button(
      "Expand editor",
      () => {
        const expanded = note.classList.toggle("expanded");
        expand.textContent = expanded ? "Collapse editor" : "Expand editor";
        expand.setAttribute("aria-expanded", String(expanded));
      },
      "secondary",
    );
    expand.setAttribute("aria-expanded", "false");
    controls.append(expand, submit);
    form.append(field("Executor", machine), buttons, preview, reason, controls);
    form.addEventListener("commandconfirmed", (event) => {
      const sent = event.detail;
      if (
        sent.path === "execution" &&
        sent.body.action === action &&
        sent.body.note === note.value &&
        sent.body.executor === machine.value
      )
        delete form.dataset.dirty;
    });
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!action || !form.reportValidity()) return;
      const ok = await send("execution", {
        plat: p.id,
        executor: machine.value,
        action,
        version: p.version,
        scope_hash: action === "refresh_delivery" ? execution.delivery_scope_hash : execution.scope_hash,
        status: p.status,
        note: note.value,
      });
      if (ok) {
        delete form.dataset.dirty;
        await refresh(false);
      }
    };
    root.append(form);
  }
  root.append(node("h3", "Control requests"));
  if (!execution.commands.some(c => !deliveryOnly || c.action === "refresh_delivery"))
    root.append(node("p", deliveryOnly ? "No provider refresh has been requested." : "No execution controls have been requested."));
  for (const c of execution.commands.filter(c => !deliveryOnly || c.action === "refresh_delivery")) {
    const item = node("article", undefined, "card");
    item.append(
      node(
        "h4",
        `${c.action.toUpperCase()} · ${c.state}${c.connection_lost ? " · connection lost; outcome uncertain" : ""}`,
      ),
      node("p", `${c.actor} → ${c.executor}`),
      node("p", c.note, "prose"),
      node(
        "p",
        `Requested ${when(c.requested)} · Acknowledged ${when(c.acknowledged)} · Started ${when(c.started)} · Finished ${when(c.finished)}`,
        "meta",
      ),
      node("p", c.outcome || "Waiting for the owning machine to acknowledge."),
    );
    if (c.state === "requested" && member.role !== "viewer")
      item.append(
        button(
          "Cancel queued request",
          async () => {
            await send("execution", {
              action: "cancel",
              plat: p.id,
              command: c.id,
            });
          },
          "secondary",
        ),
      );
    root.append(item);
  }
  return root;
}

function deliveryPanel(p, d) {
  const root = node("section");
  root.append(node("h3", "Delivery workspace"), node("p",
    d.readiness.ready ? "Lifecycle evidence complete" : d.readiness.missing.join(" · ")));
  root.append(node("p", "External review and CI are observations, not Plat gate results. Request a refresh below or use the owner CLI; observations older than 15 minutes are flagged.", "meta"));
  root.append(table(["Evidence", "Status", "Version", "SHA-256"],
    d.stages.map(s => [s.stage === "recording" ? "Delivery package" : s.stage, s.status, s.version, s.sha256])));
  const integration = d.stages.find(s => s.stage === "integration");
  if (integration?.checks?.length) root.append(node("h4", "Recorded integration checks"),
    table(["Check", "Exit code"], integration.checks.map(c => [c.name, c.exit_code])));
  for (const lot of d.lots) {
    const card = node("section", undefined, "item");
    card.append(node("h3", lot.key), node("p", lot.revision
      ? lot.revision.commit + (lot.revision.dirty ? " · uncommitted changes" : " · clean")
      : "Current revision unavailable", "meta"));
    if (lot.url) {
      const a = node("a", lot.url); a.href = lot.url; a.target = "_blank"; a.rel = "noopener noreferrer";
      card.append(a);
    }
    const o = lot.observation;
    if (o.observed_at) card.append(node("p", "Last observed: " + when(o.observed_at), "meta"));
    if (o.head) card.append(table(["PR/MR", "Review", "Head revision"],
      [[o.state + (o.draft ? " · draft" : ""), o.review, o.head]]));
    if (o.checks?.length) card.append(table(["CI check", "Observed result"], o.checks.map(c => [c.name, c.status])));
    for (const warning of lot.warnings) card.append(node("p", warning, "error"));
    card.append(node("p", "Refresh from the owning laptop:"), node("code", "plat delivery refresh " + p.id + " " + lot.key));
    if (member.role !== "viewer") {
      const form = node("form"), input = textInput(lot.url || "", 500);
      input.type = "url"; input.required = false;
      form.append(field("GitHub PR / GitLab MR URL (clear to unlink)", input));
      form.addEventListener("input", () => form.dataset.dirty = "true");
      const save = node("button", "Save link", "secondary"); save.type = "submit";
      save.disabled = !connected; form.append(save);
      form.addEventListener("commandconfirmed", event => {
        if (event.detail.path === "workspace" && event.detail.body.action === "delivery_link" && event.detail.body.lot === lot.key)
          delete form.dataset.dirty;
      });
      form.onsubmit = async e => {
        e.preventDefault(); save.disabled = true;
        await send("workspace", {action: "delivery_link", plat: p.id, lot: lot.key, url: input.value.trim(), version: lot.version});
        save.disabled = !connected;
      };
      card.append(form);
    }
    root.append(card);
  }
  return root;
}
