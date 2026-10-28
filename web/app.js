/* AgriGuard web dashboard – JavaScript thuần, gọi REST API tương đối ("api/...")
   để chạy được cả ở gốc domain lẫn sau reverse proxy (vd. synsen.net/agriguard/). */
(() => {
  const TZ = "Asia/Ho_Chi_Minh";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const fmtDateTime = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const num = (v, d = 1) => (v == null ? "–" : Number(v).toLocaleString("vi-VN", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* bỏ qua */ } },
  };

  const S = {
    meta: { incident_types: {} }, devices: [], selected: store.get("agri.device"), incidents: [],
    settings: null, range: 60, tab: "history", commands: [], busy: new Set(),
  };

  const REASONS = {
    soil_dry: "đất khô", hot_day: "trời nóng", air_dry: "không khí khô", soil_target_reached: "đất đủ ẩm",
    max_runtime: "quá thời gian", too_humid: "quá ẩm", comfort_reached: "đã mát & đủ ẩm", manual: "thủ công",
    verification: "kiểm chứng sự cố", incident_interlock: "khoá do sự cố", mode_off: "chế độ tắt",
    soil_sensor_unavailable: "mất cảm biến đất", dht_unavailable: "mất cảm biến DHT11",
  };
  const ACTIONS = { on: "Bật", off: "Tắt", clear_lockout: "Gỡ khoá" };
  const CMD_STATUS = { pending: "đang gửi", sent: "đã gửi, chờ xác nhận", acked: "đã xác nhận", rejected: "bị từ chối", timeout: "không phản hồi", failed: "gửi lỗi" };
  const INC_STATUS = { open: "Mới", acknowledged: "Đã tiếp nhận", verifying: "Đang kiểm chứng", resolved: "Đã khắc phục" };
  const DETECTORS = { rules: "luật hệ thống", device: "thiết bị", datadog: "Datadog" };
  const SEVERITY = { critical: ["critical", "Nghiêm trọng"], warning: ["warning", "Cảnh báo"], info: ["idle", "Thông tin"] };
  const TL_ACTIONS = {
    opened: "Mở sự cố", detected: "Phát hiện thêm", acknowledged: "Tiếp nhận", resolve_requested: "Báo đã xử lý",
    verification_started: "Bắt đầu kiểm chứng", verified: "Kiểm chứng đạt – đóng sự cố", verification_failed: "Kiểm chứng KHÔNG đạt",
    condition_cleared: "Điều kiện lỗi đã hết", condition_returned: "Lỗi xuất hiện lại", monitor_recovered: "Datadog: monitor OK", note: "Ghi chú",
    datadog_signal: "Đã gửi sang Datadog", datadog_trace: "Datadog APM trace",
  };

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}, retry = true) {
    const headers = { "Content-Type": "application/json" };
    const token = store.get("agri.token");
    if (token) headers["X-Admin-Token"] = token;
    const res = await fetch("api/" + path, { ...opts, headers });
    if (res.status === 401 && retry) {
      const t = prompt("Thao tác này cần token quản trị (ADMIN_TOKEN):");
      if (t) { store.set("agri.token", t); return api(path, opts, false); }
    }
    if (!res.ok) {
      let msg = res.statusText;
      try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* */ }
      throw new Error(msg);
    }
    return res.json();
  }
  const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
  const put = (p, body) => api(p, { method: "PUT", body: JSON.stringify(body || {}) });

  function toast(msg, ms = 3200) {
    const t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
  }

  function ago(iso) {
    if (!iso) return "–";
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return `${Math.round(s)} giây trước`;
    if (s < 3600) return `${Math.round(s / 60)} phút trước`;
    return `${Math.round(s / 3600)} giờ trước`;
  }
  const dur = (s) => (s == null ? "–" : s < 90 ? `${Math.round(s)} giây` : s < 5400 ? `${Math.round(s / 60)} phút` : `${(s / 3600).toFixed(1)} giờ`);
  const device = () => S.devices.find((d) => d._id === S.selected);
  const fresh = (d) => d && d.online && d.last_seen && Date.now() - new Date(d.last_seen) < 30000;

  // ------------------------------------------------------------------ render: header
  function renderTabs() {
    $("#device-tabs").innerHTML = S.devices.map((d) => `
      <button data-dev="${esc(d._id)}" class="${d._id === S.selected ? "on" : ""}">
        <span class="dot ${fresh(d) ? "ok" : "bad"}" aria-label="${fresh(d) ? "online" : "offline"}"></span>
        ${esc(d._id)} <span class="tag">${d.source === "sim" ? "giả lập" : "thật"}</span>
      </button>`).join("") || '<span class="muted small">Chưa có thiết bị nào gửi dữ liệu</span>';
  }

  function renderBanner() {
    const crit = S.incidents.filter((i) => i.severity === "critical" && i.status !== "verifying");
    const b = $("#banner");
    b.hidden = !crit.length;
    if (crit.length) b.textContent = `⚠ ${crit.length} sự cố nghiêm trọng đang mở: ` + crit.map((i) => `${i.title} (${i.device_id})`).join(" · ");
  }

  // ------------------------------------------------------------------ render: KPIs
  function statusTag(cls, label) { return `<span class="status ${cls}">${esc(label)}</span>`; }

  function fmtDuration(min) {
    if (min == null) return "–";
    if (min < 1) return "< 1 phút";
    if (min < 120) return `${Math.round(min)} phút`;
    if (min < 48 * 60) return `${(min / 60).toFixed(1)} giờ`;
    return `${Math.round(min / 1440)} ngày`;
  }
  function dryHint(p) {
    if (!p || p.minutes_to_dry == null) return "";
    return p.minutes_to_dry <= 0 ? " · đã tới ngưỡng tưới" : ` · dự báo khô tới ngưỡng sau ~${fmtDuration(p.minutes_to_dry)}`;
  }

  function renderKpis() {
    const d = device();
    const k = $("#kpis");
    if (!d) { k.innerHTML = '<div class="card kpi muted">Chọn một thiết bị ở thanh trên.</div>'; return; }
    const L = d.latest || {}, st = d.status || {}, cfg = S.settings;
    const online = fresh(d);
    const dhtOk = L.dht_ok !== false && L.air_temp_c != null;
    let soilTag = statusTag("good", "Bình thường");
    if (L.soil_ok === false) soilTag = statusTag("critical", "Lỗi cảm biến");
    else if (cfg && L.soil_moisture_pct != null && L.soil_moisture_pct < cfg.pump.soil_dry_pct) soilTag = statusTag("warning", "Khô");
    else if (cfg && L.soil_moisture_pct != null && L.soil_moisture_pct >= cfg.pump.soil_target_pct) soilTag = statusTag("good", "Đủ ẩm");
    const tile = (label, tag, value, unit, sub, fault) => `
      <div class="card kpi ${fault ? "fault" : ""}"><div class="label"><span>${label}</span>${tag}</div>
      <div class="value">${value}${unit ? `<small>${unit}</small>` : ""}</div><div class="sub">${sub}</div></div>`;
    k.innerHTML = [
      dhtOk ? tile("Nhiệt độ", statusTag("good", "DHT11 OK"), num(L.air_temp_c), "°C", cfg ? `Ngưỡng nóng ${cfg.mist.hot_temp_c}°C` : "")
            : tile("Nhiệt độ", statusTag("critical", "Lỗi"), "Mất tín hiệu", "", "DHT11 không trả dữ liệu", true),
      dhtOk ? tile("Độ ẩm không khí", "", num(L.air_humidity_pct, 0), "%", cfg ? `Phun khi < ${cfg.mist.air_dry_pct}% · tối đa ${cfg.mist.air_max_pct}%` : "")
            : tile("Độ ẩm không khí", statusTag("critical", "Lỗi"), "Mất tín hiệu", "", "", true),
      L.soil_ok === false ? tile("Độ ẩm đất", soilTag, "Lỗi", "", `ADC = ${L.soil_raw ?? "–"}`, true)
            : tile("Độ ẩm đất", soilTag, num(L.soil_moisture_pct, 0), "%",
                   (cfg ? `Tưới < ${cfg.pump.soil_dry_pct}% · dừng ≥ ${cfg.pump.soil_target_pct}%` : "") +
                   (L.soil_raw != null ? ` · ADC ${L.soil_raw}` : "") + dryHint(d.prediction)),
      tile("Lưu lượng nước", L.pump_on ? statusTag("good", "Bơm chạy") : statusTag("idle", "Bơm tắt"), num(L.flow_lpm, 2), "L/phút",
           L.pump_on && L.flow_lpm < (cfg?.pump.min_flow_lpm ?? 0.3) ? "⚠ Bơm chạy nhưng không có nước" : "YF‑S201"),
      tile("Thiết bị", online ? statusTag("good", "Online") : statusTag("critical", "Offline"), esc(d._id), "",
           `${st.rssi_dbm != null ? `Wi‑Fi ${st.rssi_dbm} dBm · ` : ""}cập nhật ${ago(d.last_seen)}${st.reset_reason ? ` · khởi động: ${esc(st.reset_reason)}` : ""}`),
    ].join("");
  }

  // ------------------------------------------------------------------ render: actuators
  function lastCommand(act) {
    const c = S.commands.find((x) => x.actuator === act);
    if (!c) return "Chưa có lệnh";
    const lat = c.latency_ms != null ? ` sau ${c.latency_ms} ms` : "";
    return `Lệnh gần nhất: <b>${ACTIONS[c.action] || c.action}</b> · ${esc(REASONS[c.reason] || c.reason)} · ${CMD_STATUS[c.status] || c.status}${lat} · ${fmtTime.format(new Date(c.created_at))}`;
  }

  function renderActuator(act) {
    const d = device(), box = $(`#act-${act}`);
    if (!d) { box.innerHTML = ""; return; }
    const a = (d.actuators || {})[act] || {};
    const name = act === "pump" ? "Bơm tưới gốc" : "Máy phun sương";
    const hint = act === "pump" ? "Tự động theo độ ẩm đất (và trời nóng)" : "Tự động theo nhiệt độ & độ ẩm không khí";
    const on = a.state === "on", lock = a.lockout;
    const stateText = lock ? "KHOÁ AN TOÀN" : on ? "ĐANG CHẠY" : "TẮT";
    const modes = [["AUTO", "Tự động"], ["MANUAL", "Thủ công"], ["OFF", "Tắt hẳn"]];
    const dis = !fresh(d) ? "disabled" : "";
    box.innerHTML = `
      <div class="head"><div><h3>${name}</h3><div class="muted small">${hint}</div></div>
        <div class="seg" role="group" aria-label="Chế độ">${modes.map(([m, l]) => `<button data-mode="${m}" data-act="${act}" class="${a.mode === m ? "on" : ""}">${l}</button>`).join("")}</div>
      </div>
      <div class="state ${lock ? "lock" : on ? "on" : ""}">${stateText}</div>
      <div class="row">
        <select data-dur="${act}" aria-label="Thời gian chạy" style="width:auto">
          ${durOptions(act)}
        </select>
        <button class="primary" data-cmd="on" data-act="${act}" ${dis || (lock ? "disabled" : "")}>Bật</button>
        <button data-cmd="off" data-act="${act}" ${dis}>Tắt</button>
        ${lock ? `<button class="danger" data-cmd="clear_lockout" data-act="${act}" ${dis}>Gỡ khoá</button>` : ""}
      </div>
      <div class="last">${lastCommand(act)}</div>
      ${a.mode === "AUTO" || (d.control || {})[act]?.reason === "incident_block" ? `<div class="last why">${controlWhy(d, act)}</div>` : ""}`;
  }

  // Thời lượng bật tay: bơm có thêm mức giây (chậu demo); không vượt "chạy tối đa mỗi lần"
  function durOptions(act) {
    const opts = act === "pump" ? [3, 5, 10, 30, 60, 120, 300, 600] : [60, 120, 300, 600];
    const max = act === "pump" ? S.settings?.pump?.max_seconds : null;
    const list = max ? [...new Set([...opts.filter((s) => s < max), max])] : opts;
    const picked = (S.dur || {})[act];
    const def = list.includes(picked) ? picked : act === "pump" ? (max && max < 120 ? max : 120) : 120;
    const lab = (s) => (s < 60 || s % 60 ? `${s} giây` : `${s / 60} phút`);
    return list.map((s) => `<option value="${s}" ${s === def ? "selected" : ""}>${lab(s)}</option>`).join("");
  }

  // Vì sao bộ điều khiển tự động đang bật / chưa bật (backend ghi vào devices.control)
  const WHY = {
    offline: "thiết bị mất kết nối", stale_data: "dữ liệu cũ quá 30 giây", verifying: "đang kiểm chứng sự cố",
    mode_manual: "đang ở chế độ Thủ công", mode_off: "đang ở chế độ Tắt hẳn", pending_command: "đang chờ thiết bị xác nhận lệnh trước",
    incident_block: "bị khoá do sự cố đang mở", device_lockout: "thiết bị đang khoá an toàn – bấm Gỡ khoá",
    dht_unavailable: "cảm biến DHT11 chưa có số đo", soil_unavailable: "cảm biến đất chưa có số đo hợp lệ",
    running: "đang chạy, chờ điều kiện tắt", too_humid: "độ ẩm không khí đã ≥ ngưỡng tối đa",
    min_off: "đang trong thời gian nghỉ tối thiểu", no_data: "chưa có dữ liệu",
    decided_on: "vừa ra lệnh BẬT", decided_off: "vừa ra lệnh TẮT",
  };
  function controlWhy(d, act) {
    const c = (d.control || {})[act];
    if (!c) return "Tự động: chưa có đánh giá";
    const s = S.settings, r = d.latest || {};
    let text = WHY[c.reason] || c.reason;
    if (c.reason === "incident_block" && c.incidents?.length) text += ` (${c.incidents.join(", ")})`;
    if (c.decision) text += ` – ${c.decision}`;
    if (c.reason === "conditions_not_met" && s) {
      text = act === "mist"
        ? `chưa đạt điều kiện: cần nhiệt độ > ${s.mist.hot_temp_c}°C hoặc độ ẩm KK < ${s.mist.air_dry_pct}%`
        : `chưa đạt điều kiện: cần đất < ${s.pump.soil_dry_pct}%`;
    }
    return `Tự động: ${text}`;
  }

  // ------------------------------------------------------------------ render: charts
  async function loadCharts() {
    const d = device();
    if (!d) return;
    try {
      const [series, today] = await Promise.all([
        api(`devices/${encodeURIComponent(d._id)}/telemetry?minutes=${S.range}`),
        api(`devices/${encodeURIComponent(d._id)}/stats/today`),
      ]);
      const pts = series.points, to = Date.now(), from = to - S.range * 60000;
      const stepMs = series.bucket_s * 1000;
      const cfg = S.settings;
      const common = { points: pts, from, to, stepMs };
      LineChart.draw($("#ch-temp"), { ...common, key: "air_temp_c", color: "var(--series-temp)", unit: "°C", label: "Nhiệt độ",
        bandKey: "mist_on", bandLabel: "Phun sương", minSpan: 4,
        thresholds: cfg ? [{ v: cfg.mist.hot_temp_c, label: `nóng ${cfg.mist.hot_temp_c}°C` }] : [] });
      LineChart.draw($("#ch-hum"), { ...common, key: "air_humidity_pct", color: "var(--series-hum)", unit: "%", decimals: 0, label: "Độ ẩm không khí",
        bandKey: "mist_on", bandLabel: "Phun sương", minSpan: 10, floor: 0 });
      LineChart.draw($("#ch-soil"), { ...common, key: "soil_moisture_pct", color: "var(--series-soil)", unit: "%", decimals: 0, label: "Độ ẩm đất",
        bandKey: "pump_on", bandLabel: "Bơm", bandColor: "var(--band-pump)", minSpan: 10, floor: 0,
        thresholds: cfg ? [{ v: cfg.pump.soil_dry_pct, label: `khô ${cfg.pump.soil_dry_pct}%` }, { v: cfg.pump.soil_target_pct, label: `đủ ${cfg.pump.soil_target_pct}%` }] : [] });
      LineChart.draw($("#ch-flow"), { ...common, key: "flow_lpm", color: "var(--series-flow)", unit: "L/phút", decimals: 2, label: "Lưu lượng",
        bandKey: "pump_on", bandLabel: "Bơm", bandColor: "var(--band-pump)", yMin: 0, yMax: 2, floor: 0 });
      $("#today").innerHTML = `
        <span>Hôm nay: nước đã tưới <b>${num(today.water_liters, 1)} L</b></span>
        <span>bơm chạy <b>${num(today.pump_minutes, 1)} phút</b> (${today.pump_starts} lần bật)</span>
        <span>phun sương <b>${num(today.mist_minutes, 1)} phút</b></span>
        <span>nhiệt độ <b>${num(today.temp_min)}–${num(today.temp_max)} °C</b></span>
        ${d.prediction?.liters_per_day != null ? `<span title="tốc độ khô của đất ÷ phản hồi đất trên mỗi lít (đã học)">dự báo cần <b>${num(d.prediction.liters_per_day, 1)} L/ngày</b> · lần tưới tới ~<b>${num(d.prediction.liters_next_irrigation, 2)} L</b></span>` : ""}`;
    } catch (e) { console.warn(e); }
  }

  // ------------------------------------------------------------------ render: incidents
  function incidentCard(i) {
    const [sevCls, sevLabel] = SEVERITY[i.severity] || SEVERITY.info;
    const busy = S.busy.has(i._id);
    const dd = i.datadog?.link ? `<a class="chip datadog" href="${esc(i.datadog.link)}" target="_blank" rel="noopener">Mở trong Datadog ↗</a>` : "";
    const actions = i.status === "verifying"
      ? `<span class="small"><span class="spinner"></span> Hệ thống đang chạy thử và đo dữ liệu để kiểm chứng…</span>`
      : `${i.status === "open" ? `<button data-ack="${i._id}" ${busy ? "disabled" : ""}>Tiếp nhận</button>` : ""}
         <button class="primary" data-resolve="${i._id}" ${busy ? "disabled" : ""}>Đã xử lý – kiểm chứng</button>`;
    const tl = timelineHtml(i);
    return `<div class="incident ${sevCls}" data-id="${esc(i._id)}">
      <div class="line1">${statusTag(sevCls, sevLabel)}<span class="title">${esc(i.title)}</span>
        <span class="chip">${esc(i.device_id)}</span><span class="chip st-${i.status}">${INC_STATUS[i.status] || i.status}</span>
        ${i.condition_active === false ? '<span class="chip">dữ liệu đã bình thường</span>' : ""}${dd}</div>
      <div class="msg">${esc(i.message)}</div>
      <div class="meta"><span>Mở lúc ${fmtDateTime.format(new Date(i.opened_at))} (${ago(i.opened_at)})</span>
        <span>Phát hiện bởi: ${(i.detected_by || []).map((x) => DETECTORS[x] || x).join(", ")}</span>
        <span>MTTD ${dur(i.mttd_s)}</span><span class="muted">${esc(i._id)}</span></div>
      ${ddLinks(i)}
      <details><summary class="small">Dòng thời gian điều tra (${(i.timeline || []).length})</summary><ul class="timeline">${tl}</ul></details>
      <div class="actions">${actions}</div></div>`;
  }

  // ---- Datadog trong quy trình điều tra: link sâu tới đúng monitor / log / sự kiện / trace của sự cố
  const ddApp = () => S.meta?.datadog_app;
  function ddLinks(i) {
    const app = ddApp();
    if (!app) return "";
    const from = new Date(i.fault_started_at || i.opened_at).getTime() - 10 * 60e3;
    const to = (i.resolved_at ? new Date(i.resolved_at).getTime() : Date.now()) + 10 * 60e3;
    const q = (s) => encodeURIComponent(s);
    const links = [
      ["Monitor", `https://${app}/monitors/manage?q=${q(`tag:"incident_type:${i.type}"`)}`],
      ["Logs", `https://${app}/logs?query=${q(`"${i._id}"`)}&from_ts=${from}&to_ts=${to}&live=false`],
      ["Sự kiện", `https://${app}/event/explorer?query=${q(`incident_id:${i._id}`)}&from_ts=${from}&to_ts=${to}&live=false`],
      ["Metric thiết bị", `https://${app}/metric/explorer?exp_metric=${q("agriguard.flow.rate,agriguard.pump.state,agriguard.soil.moisture")}&exp_scope=${q(`device_id:${i.device_id}`)}&from_ts=${from}&to_ts=${to}&live=false`],
    ];
    return `<div class="ddlinks"><span class="small muted">Điều tra trên Datadog:</span>${links.map(([l, u]) =>
      `<a class="chip datadog" href="${u}" target="_blank" rel="noopener">${l} ↗</a>`).join("")}</div>`;
  }
  function timelineHtml(i) {
    const app = ddApp();
    return (i.timeline || []).map((t) => {
      const link = t.trace_id && app ? `https://${app}/apm/trace/${t.trace_id}` : t.link;
      const a = link ? ` <a href="${esc(link)}" target="_blank" rel="noopener">${t.trace_id ? "mở trace" : "mở monitor"} ↗</a>` : "";
      return `<li class="${t.actor === "datadog" ? "dd" : ""}"><time>${fmtTime.format(new Date(t.at))}</time><b>${esc(TL_ACTIONS[t.action] || t.action)}</b>
        <span class="muted">(${esc(DETECTORS[t.actor] || t.actor)})</span> ${esc(t.note || "")}${a}</li>`;
    }).join("");
  }

  function renderIncidents() {
    const order = { critical: 0, warning: 1, info: 2 };
    const list = [...S.incidents].sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3) || new Date(b.opened_at) - new Date(a.opened_at));
    $("#inc-count").textContent = list.length;
    const box = $("#incidents");
    const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.closest(".incident")?.dataset.id));
    box.innerHTML = list.length ? list.map(incidentCard).join("") : '<div class="none">✓ Không có sự cố nào đang mở.</div>';
    box.querySelectorAll(".incident").forEach((el) => { if (open.has(el.dataset.id)) el.querySelector("details").open = true; });
  }

  // ------------------------------------------------------------------ tabs
  async function renderHistory() {
    const [rows, stats] = await Promise.all([api("incidents?status=resolved&limit=50"), api("incidents/stats")]);
    const types = S.meta.incident_types;
    const statRows = stats.by_type.map((s) => `<tr><td>${esc(types[s.type]?.title || s.type)}</td><td>${s.count}</td><td>${dur(s.avg_mttd_s)}</td><td>${dur(s.avg_mttr_s)}</td><td>${dur(s.max_mttr_s)}</td></tr>`).join("");
    $("#tab-history").innerHTML = `
      <h3 class="small">Thống kê theo loại sự cố</h3>
      <div class="table-wrap"><table><thead><tr><th>Loại</th><th>Số lần</th><th>MTTD TB</th><th>MTTR TB</th><th>MTTR lớn nhất</th></tr></thead>
      <tbody>${statRows || '<tr><td colspan="5" class="muted">Chưa có sự cố nào được khắc phục</td></tr>'}</tbody></table></div>
      <h3 class="small">Sự cố đã khắc phục gần đây</h3>
      <div class="table-wrap"><table><thead><tr><th>Mã</th><th>Thiết bị</th><th>Loại</th><th>Phát hiện bởi</th><th>Mở</th><th>Đóng</th><th>MTTD</th><th>MTTR</th><th>Bằng chứng kiểm chứng</th></tr></thead>
      <tbody>${rows.map((i) => `<tr><td>${esc(i._id)}</td><td>${esc(i.device_id)}</td><td>${esc(i.title)}</td><td>${(i.detected_by || []).map((x) => DETECTORS[x] || x).join(", ")}</td>
        <td>${fmtDateTime.format(new Date(i.opened_at))}</td><td>${i.resolved_at ? fmtDateTime.format(new Date(i.resolved_at)) : "–"}</td>
        <td>${dur(i.mttd_s)}</td><td>${dur(i.mttr_s)}</td><td>${esc(i.verification || "")}
        <details><summary class="small">Dòng thời gian (${(i.timeline || []).length})</summary><ul class="timeline">${timelineHtml(i)}</ul>${ddLinks(i)}</details></td></tr>`).join("") || '<tr><td colspan="9" class="muted">Trống</td></tr>'}</tbody></table></div>`;
  }

  function renderCommands() {
    $("#tab-commands").innerHTML = `<div class="table-wrap"><table><thead><tr><th>Thời gian</th><th>Thiết bị</th><th>Thiết bị chấp hành</th><th>Lệnh</th><th>Lý do</th><th>Người/nguồn</th><th>Trạng thái</th><th>Độ trễ ack</th></tr></thead>
      <tbody>${S.commands.map((c) => `<tr><td>${fmtDateTime.format(new Date(c.created_at))}</td><td>${esc(c.device_id)}</td><td>${c.actuator === "pump" ? "Bơm" : "Phun sương"}</td>
        <td>${ACTIONS[c.action] || c.action}${c.duration_s ? ` (${Math.round(c.duration_s / 60)}′)` : ""}</td><td>${esc(REASONS[c.reason] || c.reason)}</td><td>${esc(c.actor)}</td>
        <td>${CMD_STATUS[c.status] || c.status}</td><td>${c.latency_ms != null ? c.latency_ms + " ms" : "–"}</td></tr>`).join("") || '<tr><td colspan="8" class="muted">Chưa có lệnh</td></tr>'}</tbody></table></div>`;
  }

  const SETTING_FIELDS = {
    pump: ["Bơm tưới", [["soil_dry_pct", "Đất khô – bắt đầu tưới (%)"], ["soil_target_pct", "Đất đủ ẩm – dừng tưới (%)"], ["hot_temp_c", "Trời nóng – tưới sớm (°C)"],
      ["max_seconds", "Chạy tối đa mỗi lần (giây)"], ["min_off_minutes", "Nghỉ tối thiểu (phút)"], ["min_flow_lpm", "Lưu lượng tối thiểu (L/phút)"],
      ["dry_run_seconds", "Báo chạy khô sau (giây)"], ["leak_seconds", "Báo rò/kẹt relay sau (giây)"]]],
    mist: ["Phun sương", [["hot_temp_c", "Trời nóng – bật phun (°C)"], ["temp_hysteresis_c", "Độ trễ nhiệt (°C)"], ["air_dry_pct", "Không khí khô – bật phun (%)"],
      ["air_target_pct", "Độ ẩm mục tiêu (%)"], ["air_max_pct", "Độ ẩm tối đa – không phun (%)"], ["max_minutes", "Chạy tối đa mỗi lần (phút)"],
      ["min_off_minutes", "Nghỉ tối thiểu (phút)"], ["check_minutes", "Kiểm tra hiệu quả sau (phút)"], ["min_rise_pct", "Độ ẩm phải tăng ít nhất (%)"]]],
    irrigation: ["Phản hồi đất / lít nước", [["settle_seconds", "Chờ nước thấm sau khi tắt bơm (giây)"], ["before_seconds", "Lấy độ ẩm “trước” trong (giây)"], ["after_window_seconds", "Lấy độ ẩm “sau” trong (giây)"], ["min_baseline_events", "Số lần tưới để học mức bình thường"],
      ["baseline_events", "Số lần tưới gần nhất dùng làm chuẩn"], ["zscore_threshold", "Ngưỡng bất thường |z|"], ["min_liters", "Lượng nước tối thiểu để đánh giá (L)"],
      ["max_start_soil_pct", "Không đánh giá khi đất đã ẩm hơn (%)"], ["test_seconds", "Tưới thử khi kiểm chứng (giây)"]]],
    health: ["Giám sát", [["offline_after_s", "Coi là offline sau (giây)"], ["stuck_minutes", "Cảm biến đơ sau (phút)"], ["sensor_fail_count", "Số lần đọc lỗi liên tiếp"], ["rssi_weak_dbm", "Wi‑Fi yếu dưới (dBm)"]]],
  };

  function renderSettings() {
    const s = S.settings;
    if (!s) return;
    $("#tab-settings").innerHTML = `<form id="settings-form"><div class="settings">${Object.entries(SETTING_FIELDS).map(([grp, [title, fields]]) => `
      <fieldset><legend>${title}</legend>${fields.map(([k, label]) => `<label>${label}<input type="number" step="any" name="${grp}.${k}" value="${s[grp][k]}"></label>`).join("")}</fieldset>`).join("")}</div>
      <div class="row" style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="primary" type="submit">Lưu cài đặt</button>
      <span class="muted small">Áp dụng ngay cho bộ điều khiển tự động và luật phát hiện sự cố.</span></div></form>`;
  }

  const FAULTS = [
    ["dry_run", "Bơm chạy khô", "Hết nước / tắc ống: bơm chạy nhưng lưu lượng = 0"],
    ["leak", "Rò nước / relay kẹt", "Có dòng chảy khi bơm đang tắt"],
    ["mist_empty", "Máy phun hết nước", "Phun sương không làm tăng độ ẩm"],
    ["hose_off", "Ống tưới tuột khỏi gốc", "Bơm chạy, có lưu lượng nhưng đất không ẩm lên (phát hiện sau lần tưới kế tiếp)"],
    ["dht_fail", "DHT11 hỏng", "Cảm biến nhiệt độ/độ ẩm trả lỗi"],
    ["soil_disconnect", "Tuột dây cảm biến đất", "ADC = 4095"],
    ["soil_stuck", "Cảm biến đất bị đơ", "Giá trị không đổi (phát hiện sau ~10 phút)"],
    ["offline", "Mất điện / mất Wi‑Fi", "Thiết bị ngắt kết nối, broker phát LWT"],
    ["no_ack", "Thiết bị treo lệnh", "Không thực hiện, không xác nhận lệnh"],
    ["brownout", "Sụt áp khởi động lại", "ESP32 reset do brownout"],
    ["slow_network", "Mạng chậm", "Dữ liệu đến trễ 6–9 giây (vượt SLO)"],
  ];

  function renderSim() {
    const d = device();
    const box = $("#tab-sim");
    if (!S.meta.sim_api) { box.innerHTML = '<p class="muted">API mô phỏng lỗi đang tắt (ENABLE_SIM_API=false).</p>'; return; }
    if (!d || d.source !== "sim") { box.innerHTML = '<p class="muted">Chọn một thiết bị <b>giả lập</b> (sim‑xx) để tiêm lỗi. Thiết bị thật được thử lỗi bằng thao tác vật lý (nhấc ống hút, rút dây…).</p>'; return; }
    box.innerHTML = `<p class="small muted">Tiêm lỗi vào <b>${esc(d._id)}</b> (dữ liệu giả lập) để diễn tập kịch bản Phát hiện – Điều tra – Khắc phục. Để “sửa” lỗi, bấm “Xoá mọi lỗi” rồi bấm “Đã xử lý – kiểm chứng” trên sự cố.</p>
      <div class="sim">${FAULTS.map(([f, t, h]) => `<button data-fault="${f}"><b>${t}</b><span>${h}</span></button>`).join("")}
      <button data-fault="clear" class="primary"><b>Xoá mọi lỗi (sửa xong)</b><span>Thiết bị trở lại bình thường</span></button></div>`;
  }

  function renderTab() {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab));
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== `tab-${S.tab}`));
    if (S.tab === "history") renderHistory().catch(console.warn);
    if (S.tab === "commands") renderCommands();
    if (S.tab === "settings") renderSettings();
    if (S.tab === "sim") renderSim();
  }

  // ------------------------------------------------------------------ data loops
  async function refresh() {
    try {
      const [devices, incidents] = await Promise.all([api("devices"), api("incidents?status=active")]);
      S.devices = devices; S.incidents = incidents;
      if (!S.selected || !devices.some((d) => d._id === S.selected)) S.selected = devices[0]?._id || null;
      if (S.selected) S.commands = await api(`commands?device_id=${encodeURIComponent(S.selected)}&limit=25`);
      renderTabs(); renderBanner(); renderKpis(); renderActuator("pump"); renderActuator("mist"); renderIncidents();
      if (S.tab === "commands") renderCommands();
    } catch (e) {
      $("#sys-status").textContent = "Mất kết nối máy chủ";
    }
  }

  // Sau khi gửi lệnh: làm mới dày trong vài giây đầu để thấy ack + trạng thái mới ngay
  function afterCommand() {
    [400, 1200, 2500, 5000].forEach((ms) => setTimeout(refresh, ms));
    [1500, 6000].forEach((ms) => setTimeout(loadCharts, ms));
  }

  async function refreshHealth() {
    try {
      const h = await api("health");
      $("#sys-status").textContent = `MongoDB ${h.mongo === "ok" ? "✓" : "✗"} · MQTT ${h.mqtt === "connected" ? "✓" : "✗"} · Datadog ${h.datadog_metrics ? "✓" : "tắt"}`;
      $("#sys-status").title = `timeseries=${h.timeseries}, env=${h.env}`;
    } catch { $("#sys-status").textContent = "Mất kết nối máy chủ"; }
  }

  // ------------------------------------------------------------------ events
  // Giữ lựa chọn thời lượng khi trang tự làm mới
  document.addEventListener("change", (ev) => {
    const sel = ev.target.closest("select[data-dur]");
    if (sel) { S.dur = S.dur || {}; S.dur[sel.dataset.dur] = +sel.value; }
  });

  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    const d = device();
    try {
      if (b.dataset.dev) {
        S.selected = b.dataset.dev; store.set("agri.device", S.selected);
        await refresh(); loadCharts(); if (S.tab === "sim") renderSim();
      } else if (b.dataset.min) {
        S.range = +b.dataset.min;
        document.querySelectorAll("#range button").forEach((x) => x.classList.toggle("on", x === b));
        loadCharts();
      } else if (b.dataset.tab) {
        S.tab = b.dataset.tab; renderTab();
      } else if (b.dataset.mode && d) {
        await put(`devices/${encodeURIComponent(d._id)}/actuators/${b.dataset.act}/mode`, { mode: b.dataset.mode });
        toast(`Đã chuyển chế độ ${b.dataset.act === "pump" ? "bơm" : "phun sương"}: ${b.textContent}`); refresh();
      } else if (b.dataset.cmd && d) {
        const act = b.dataset.act, action = b.dataset.cmd;
        const duration = action === "on" ? +document.querySelector(`[data-dur="${act}"]`).value : 0;
        b.disabled = true;
        const c = await post(`devices/${encodeURIComponent(d._id)}/actuators/${act}/command`, { action, duration_s: duration });
        toast(`Đã gửi lệnh ${ACTIONS[action]} (${c._id}). ${action !== "clear_lockout" ? "Chế độ chuyển sang Thủ công." : ""}`);
        afterCommand();
      } else if (b.dataset.ack) {
        S.busy.add(b.dataset.ack); renderIncidents();
        await post(`incidents/${b.dataset.ack}/acknowledge`, { note: "", actor: store.get("agri.actor") || "operator" });
        S.busy.delete(b.dataset.ack); toast("Đã tiếp nhận sự cố"); refresh();
      } else if (b.dataset.resolve) {
        const note = prompt("Bạn đã xử lý gì? (vd: châm nước vào bể, cắm lại dây cảm biến)", "");
        if (note === null) return;
        S.busy.add(b.dataset.resolve); renderIncidents();
        await post(`incidents/${b.dataset.resolve}/resolve`, { note, actor: store.get("agri.actor") || "operator" });
        S.busy.delete(b.dataset.resolve); toast("Hệ thống đang kiểm chứng bằng dữ liệu thực…"); refresh();
      } else if (b.dataset.fault && d) {
        await post(`sim/${encodeURIComponent(d._id)}/fault`, { fault: b.dataset.fault, on: true });
        toast(b.dataset.fault === "clear" ? `Đã xoá mọi lỗi trên ${d._id}` : `Đã tiêm lỗi “${b.querySelector("b").textContent}” vào ${d._id}`);
      } else if (b.id === "token-btn") {
        const t = prompt("Token quản trị (để trống để xoá):", store.get("agri.token") || "");
        if (t !== null) { store.set("agri.token", t); toast(t ? "Đã lưu token" : "Đã xoá token"); }
      }
    } catch (e) {
      S.busy.clear();
      toast("Lỗi: " + e.message, 5000);
      refresh();
    }
  });

  document.addEventListener("submit", async (ev) => {
    if (ev.target.id !== "settings-form") return;
    ev.preventDefault();
    const body = {};
    new FormData(ev.target).forEach((v, k) => { const [g, f] = k.split("."); (body[g] ||= {})[f] = Number(v); });
    try {
      S.settings = await put("settings", body);
      toast("Đã lưu cài đặt"); renderKpis(); loadCharts();
    } catch (e) { toast("Không lưu được: " + e.message, 6000); }
  });

  // ------------------------------------------------------------------ boot
  function tickClock() {
    $("#clock").textContent = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date()) + " (GMT+7)";
  }

  async function boot() {
    tickClock(); setInterval(tickClock, 1000);
    try { S.meta = await api("meta"); $("#site-name").textContent = `Khu ${S.meta.site} · giám sát tưới tiêu thời gian thực`; } catch { /* */ }
    try { S.settings = await api("settings"); } catch { /* */ }
    await refresh(); refreshHealth(); loadCharts(); renderTab();
    setInterval(refresh, 3000);
    setInterval(refreshHealth, 10000);
    setInterval(loadCharts, 5000);
    setInterval(() => { if (S.tab === "history") renderHistory().catch(() => {}); }, 15000);
    setInterval(async () => { try { S.settings = await api("settings"); } catch { /* */ } }, 30000);
  }
  boot();
})();
