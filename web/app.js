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
    ai_plan: "AI: theo lịch tưới tối ưu", ai_safety_dry: "AI: đất quá khô – tưới an toàn",
    ai_fallback_soil_dry: "AI (dự phòng ngưỡng): đất khô", ai_fallback_hot_day: "AI (dự phòng ngưỡng): trời nóng",
  };
  const ACTIONS = { on: "Bật", off: "Tắt", clear_lockout: "Gỡ khoá" };
  const CMD_STATUS = { pending: "đang gửi", sent: "đã gửi, chờ xác nhận", acked: "đã xác nhận", rejected: "bị từ chối", timeout: "không phản hồi", failed: "gửi lỗi" };
  const INC_STATUS = { open: "Mới", acknowledged: "Đã tiếp nhận", verifying: "Đang kiểm chứng", resolved: "Đã khắc phục" };
  const DETECTORS = { rules: "luật hệ thống", device: "thiết bị", datadog: "Datadog", system: "hệ thống", drill: "diễn tập" };
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
    const hint = act === "pump" ? ((d.actuators || {}).pump?.mode === "AI" ? "AI quyết định khi nào tưới và tưới bao nhiêu (tab AI tưới)" : "Tự động theo độ ẩm đất (và trời nóng)") : "Tự động theo nhiệt độ & độ ẩm không khí";
    const on = a.state === "on", lock = a.lockout;
    const stateText = lock ? "KHOÁ AN TOÀN" : on ? "ĐANG CHẠY" : "TẮT";
    const modes = act === "pump" ? [["AUTO", "Tự động"], ["AI", "AI"], ["MANUAL", "Thủ công"], ["OFF", "Tắt hẳn"]]
      : [["AUTO", "Tự động"], ["MANUAL", "Thủ công"], ["OFF", "Tắt hẳn"]];
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
      ${a.mode === "AUTO" || a.mode === "AI" || (d.control || {})[act]?.reason === "incident_block" ? `<div class="last why">${controlWhy(d, act)}</div>` : ""}`;
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
    ai_settling: "chờ nước thấm để đo kết quả lần tưới", ai_learning: "đang học %/L và lưu lượng – tạm dùng ngưỡng",
    ai_no_plan: "chưa có kế hoạch mới – tạm dùng ngưỡng", ai_no_need: "chưa cần tưới trong 6 giờ tới",
  };
  function controlWhy(d, act) {
    const c = (d.control || {})[act];
    if (!c) return "Tự động: chưa có đánh giá";
    const s = S.settings, r = d.latest || {};
    let text = WHY[c.reason] || c.reason;
    if (c.reason === "incident_block" && c.incidents?.length) text += ` (${c.incidents.join(", ")})`;
    if (c.decision) text += ` – ${REASONS[c.decision] || c.decision}`;
    if (c.reason === "ai_scheduled") text = `sẽ tưới ${num(c.liters, 2)} L (${c.seconds} giây) lúc ${fmtTime.format(new Date(c.start_at)).slice(0, 5)}`;
    if (c.reason === "conditions_not_met" && s) {
      text = act === "mist"
        ? `chưa đạt điều kiện: cần nhiệt độ > ${s.mist.hot_temp_c}°C hoặc độ ẩm KK < ${s.mist.air_dry_pct}%`
        : `chưa đạt điều kiện: cần đất < ${s.pump.soil_dry_pct}%`;
    }
    return `${(d.actuators || {})[act]?.mode === "AI" ? "AI" : "Tự động"}: ${text}`;
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
      ["dry_run_seconds", "Báo chạy khô sau (giây)"], ["leak_seconds", "Báo rò/kẹt relay sau (giây)"], ["verify_seconds", "Chạy thử bơm khi kiểm chứng (giây)"]]],
    mist: ["Phun sương", [["hot_temp_c", "Trời nóng – bật phun (°C)"], ["temp_hysteresis_c", "Độ trễ nhiệt (°C)"], ["air_dry_pct", "Không khí khô – bật phun (%)"],
      ["air_target_pct", "Độ ẩm mục tiêu (%)"], ["air_max_pct", "Độ ẩm tối đa – không phun (%)"], ["max_minutes", "Chạy tối đa mỗi lần (phút)"],
      ["min_off_minutes", "Nghỉ tối thiểu (phút)"], ["check_minutes", "Kiểm tra hiệu quả sau (phút)"], ["min_rise_pct", "Độ ẩm phải tăng ít nhất (%)"],
      ["verify_rise_pct", "Kiểm chứng: độ ẩm tăng ít nhất (%)"], ["verify_temp_drop_c", "Kiểm chứng: HOẶC nhiệt độ giảm ít nhất (°C)"]]],
    irrigation: ["Phản hồi đất / lít nước", [["settle_seconds", "Chờ nước thấm sau khi tắt bơm (giây)"], ["before_seconds", "Lấy độ ẩm “trước” trong (giây)"], ["after_window_seconds", "Lấy độ ẩm “sau” trong (giây)"], ["min_baseline_events", "Số lần tưới để học mức bình thường"],
      ["baseline_events", "Số lần tưới gần nhất dùng làm chuẩn"], ["zscore_threshold", "Ngưỡng bất thường |z|"], ["detect_high_response", "Cảnh báo “đất ẩm lên bất thường” (z dương)", "bool"], ["min_liters", "Lượng nước tối thiểu để đánh giá (L)"],
      ["max_start_soil_pct", "Không đánh giá khi đất đã ẩm hơn (%)"], ["test_seconds", "Tưới thử khi kiểm chứng (giây)"]]],
    health: ["Giám sát", [["offline_after_s", "Coi là offline sau (giây)"], ["stuck_minutes", "Cảm biến đơ sau (phút)"], ["sensor_fail_count", "Số lần đọc lỗi liên tiếp"], ["rssi_weak_dbm", "Wi‑Fi yếu dưới (dBm)"],
      ["low_flow_ratio", "Báo bơm yếu khi lưu lượng < (× mức đã học)"]]],
    ai: ["AI tưới (chế độ AI của bơm)", [["horizon_hours", "Dự báo trước (giờ)"], ["train_days", "Học từ dữ liệu (ngày)"], ["min_samples", "Số mẫu tối thiểu để học"],
      ["retrain_hours", "Học lại định kỳ (giờ)"], ["plan_interval_s", "Lập lại kế hoạch mỗi (giây)"], ["plan_margin_pct", "Chỉ xét tưới khi đất ≤ ngưỡng khô + (%)"],
      ["stress_weight", "Trọng số thiếu nước (L/phút)"], ["evap_penalty", "Hệ số bay hơi khi nóng"], ["critical_margin_pct", "Tưới ngay khi đất < ngưỡng khô − (%)"],
      ["ridge_lambda", "Ridge λ (chống học thuộc)"]]],
  };

  function renderSettings() {
    const s = S.settings;
    if (!s) return;
    $("#tab-settings").innerHTML = `<form id="settings-form"><div class="settings">${Object.entries(SETTING_FIELDS).map(([grp, [title, fields]]) => `
      <fieldset><legend>${title}</legend>${fields.map(([k, label, type]) => type === "bool"
        ? `<label>${label}<select name="${grp}.${k}"><option value="1" ${s[grp][k] ? "selected" : ""}>Bật</option><option value="0" ${s[grp][k] ? "" : "selected"}>Tắt (chậu nhỏ, đất ẩm lên nhanh)</option></select></label>`
        : `<label>${label}<input type="number" step="any" name="${grp}.${k}" value="${s[grp][k]}"></label>`).join("")}</fieldset>`).join("")}</div>
      <div class="row" style="margin-top:12px;display:flex;gap:8px;align-items:center"><button class="primary" type="submit">Lưu cài đặt</button>
      <span class="muted small">Áp dụng ngay cho bộ điều khiển tự động và luật phát hiện sự cố.</span></div></form>`;
  }

  const FAULTS = [
    ["dry_run", "Bơm chạy khô", "Hết nước / tắc ống: bơm chạy nhưng lưu lượng = 0"],
    ["leak", "Rò nước / relay kẹt", "Có dòng chảy khi bơm đang tắt"],
    ["mist_empty", "Máy phun hết nước", "Phun sương không làm tăng độ ẩm"],
    ["clog", "Tắc lọc / bơm yếu", "Lưu lượng còn ~45% mức bình thường đã học (phát hiện ngay khi bơm chạy)"],
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
    if (S.tab === "metrics") renderMetrics();
    if (S.tab === "water") renderWater().catch(console.warn);
    if (S.tab === "ai") renderAi().catch(console.warn);
  }

  // ------------------------------------------------------------------ tab: Metrics
  // Danh mục metric AgriGuard gửi lên Datadog + giá trị hiện tại của thiết bị đang chọn
  const onOff = (v) => (v == null ? "–" : v ? "1 (bật)" : "0 (tắt)");
  const unit = (v, u) => (v === "–" ? "–" : `${v} ${u}`);
  const METRIC_GROUPS = [
    ["Cảm biến môi trường", [
      ["soil.moisture", "gauge", "Độ ẩm đất (%) – monitor forecast “cây sắp thiếu nước”", (d) => unit(num(d.latest?.soil_moisture_pct, 0), "%")],
      ["soil.raw", "gauge", "Số ADC thô của cảm biến đất – hiệu chuẩn, phát hiện tuột dây/chập", (d) => d.latest?.soil_raw ?? "–"],
      ["air.temperature", "gauge", "Nhiệt độ không khí (°C) từ DHT11", (d) => unit(num(d.latest?.air_temp_c), "°C")],
      ["air.humidity", "gauge", "Độ ẩm không khí (%) – kiểm tra phun sương có hiệu quả", (d) => unit(num(d.latest?.air_humidity_pct, 0), "%")],
      ["flow.rate", "gauge", "Lưu lượng nước (L/phút) từ YF‑S201", (d) => unit(num(d.latest?.flow_lpm, 2), "L/phút")],
      ["flow.volume_ml", "count", "Lượng nước (mL) mỗi bản tin – cộng dồn tổng nước; monitor anomaly lượng nước"],
      ["sensor.ok", "gauge", "1 = cảm biến đọc hợp lệ (tag sensor:dht11 | soil)", (d) => (d.latest ? `DHT11 ${d.latest.dht_ok ? 1 : 0} · đất ${d.latest.soil_ok ? 1 : 0}` : "–")],
      ["sensor.health", "gauge", "Sức khoẻ từng bộ phận theo backend (dht11, soil, flow, pump, mist)",
        (d) => (d.health ? Object.entries(d.health).map(([k, v]) => `${k} ${v === "ok" ? 1 : 0}`).join(" · ") : "–")],
    ]],
    ["Bơm và phun sương", [
      ["pump.state", "gauge", "1 = bơm đang chạy. Đặt cạnh flow.rate để thấy bơm chạy khô", (d) => onOff(d.latest?.pump_on)],
      ["pump.lockout", "gauge", "1 = ESP32 đã tự khoá bơm vì chạy khô", (d) => onOff(d.latest?.pump_lockout)],
      ["pump.runtime_seconds", "count", "Tổng giây bơm chạy – giờ chạy, hao mòn"],
      ["mist.state", "gauge", "1 = máy phun sương đang chạy", (d) => onOff(d.latest?.mist_on)],
      ["mist.runtime_seconds", "count", "Tổng giây máy phun chạy"],
      ["control.decision", "count", "Quyết định bật/tắt của bộ điều khiển tự động (tag actuator, action, reason)"],
    ]],
    ["Sức khoẻ ESP32", [
      ["device.online", "gauge", "1 = online – monitor “Thiết bị mất kết nối”", (d) => onOff(d.online)],
      ["device.uptime", "gauge", "Giây từ lần khởi động gần nhất; rơi về 0 = vừa reset", (d) => (d.status?.uptime_s != null ? `${d.status.uptime_s} s` : "–")],
      ["device.reboots", "count", "Số lần khởi động lại (tag reset_reason: brownout, watchdog…) – chỉ có sau lần reset đầu tiên", (d) => (d.status?.reset_reason ? `lần gần nhất: ${d.status.reset_reason}` : "–")],
      ["device.free_heap", "gauge", "Bộ nhớ trống (byte) – monitor forecast sắp cạn bộ nhớ", (d) => (d.status?.free_heap != null ? `${num(d.status.free_heap, 0)} B` : "–")],
      ["device.rssi", "gauge", "Cường độ Wi‑Fi (dBm); dưới −80 là yếu", (d) => (d.latest?.rssi_dbm != null ? `${d.latest.rssi_dbm} dBm` : "–")],
      ["device.wifi_reconnects", "gauge", "Số lần kết nối lại Wi‑Fi", (d) => d.status?.wifi_reconnects ?? "–"],
      ["device.mqtt_reconnects", "gauge", "Số lần kết nối lại MQTT", (d) => d.status?.mqtt_reconnects ?? "–"],
      ["device.events", "count", "Sự kiện ESP32 tự báo (tag event_type: safety_lockout, auto_off, flow_noise…)"],
    ]],
    ["Phản hồi đất và dự báo", [
      ["irrigation.response_pct_per_liter", "gauge", "Phản hồi đất của từng lần tưới (% độ ẩm / lít)", (d, x) => unit(num(x.irr?.ratio_pct_per_l, 2), "%/L")],
      ["irrigation.baseline_pct_per_liter", "gauge", "Mức bình thường học từ MongoDB", (d, x) => unit(num(x.irrBase?.median_pct_per_l, 2), "%/L")],
      ["irrigation.response_zscore", "gauge", "z‑score lần tưới gần nhất: ≤ −3 nước không tới gốc, ≥ +3 phản hồi cao bất thường", (d, x) => num(x.irr?.zscore, 2)],
      ["irrigation.liters", "gauge", "Số lít của lần tưới gần nhất", (d, x) => unit(num(x.irr?.liters, 3), "L")],
      ["irrigation.events", "count", "Số lần tưới đã chấm điểm (tag status, reason)"],
      ["soil.drying_pct_per_hour", "gauge", "Tốc độ đất khô (%/giờ), hồi quy tuyến tính", (d) => unit(num(d.prediction?.drying_pct_per_hour, 2), "%/giờ")],
      ["soil.minutes_to_dry", "gauge", "Còn bao nhiêu phút đất khô tới ngưỡng tưới", (d) => unit(num(d.prediction?.minutes_to_dry, 0), "phút")],
      ["irrigation.predicted_liters_next", "gauge", "Số lít cần cho lần tưới kế tiếp", (d) => unit(num(d.prediction?.liters_next_irrigation, 2), "L")],
      ["water.predicted_liters_per_day", "gauge", "Dự báo nước cần mỗi ngày", (d) => unit(num(d.prediction?.liters_per_day, 1), "L/ngày")],
    ]],
    ["Sự cố (Detect – Investigate – Resolve)", [
      ["fault.active", "gauge", "1 = lỗi đang tồn tại theo luật backend (tag fault) – nhiều monitor dùng, gồm “Bơm chạy khô”",
        () => { const t = [...new Set(S.incidents.filter((i) => i.device_id === S.selected).map((i) => i.type))]; return t.length ? t.join(", ") : "không có lỗi"; }],
      ["incident.open", "gauge", "Số sự cố đang mở (tag type, severity)", () => String(S.incidents.length)],
      ["incident.opened", "count", "Số sự cố mới mở"],
      ["incident.resolved", "count", "Số sự cố đã đóng sau kiểm chứng đạt"],
      ["incident.verification_failed", "count", "Số lần báo “đã sửa” mà kiểm chứng thất bại – log monitor dựa vào đây"],
      ["incident.mttd_seconds", "histogram", "MTTD: lỗi xảy ra → phát hiện"],
      ["incident.mttr_seconds", "histogram", "MTTR: mở → đóng sự cố"],
      ["incident.time_to_ack_seconds", "histogram", "Mở → người vận hành tiếp nhận"],
      ["webhook.datadog", "count", "Datadog gọi webhook về AgriGuard (tag transition)"],
    ]],
    ["Lệnh điều khiển", [
      ["command.sent", "count", "Lệnh gửi xuống thiết bị"],
      ["command.acked", "count", "Lệnh được thiết bị xác nhận"],
      ["command.acked_in_time", "count", "Lệnh xác nhận trong 10 giây – SLO lệnh"],
      ["command.timeout", "count", "Lệnh không được xác nhận – monitor “Thiết bị không xác nhận lệnh”"],
      ["command.ack_latency_ms", "histogram", "Độ trễ gửi lệnh → ack – monitor anomaly",
        () => { const c = (S.commands || []).find((x) => x.latency_ms != null); return c ? `${c.latency_ms} ms (lệnh gần nhất)` : "–"; }],
      ["command.ack_unknown", "count", "Ack không khớp lệnh nào"],
      ["command.publish_failed", "count", "Gửi lệnh lên MQTT thất bại"],
    ]],
    ["Đường ống dữ liệu và hạ tầng", [
      ["ingest.messages", "count", "Bản tin MQTT backend xử lý – monitor anomaly lượng bản tin"],
      ["ingest.latency_ms", "histogram", "Cảm biến đo → ghi xong MongoDB – monitor p95 > 5 s"],
      ["ingest.within_slo", "count", "Bản tin ghi trong 5 giây – SLO dữ liệu"],
      ["ingest.invalid_payload", "count", "Bản tin sai định dạng"],
      ["ingest.errors", "count", "Lỗi khi xử lý bản tin"],
      ["mongo.write_latency_ms", "histogram", "Thời gian ghi MongoDB"],
      ["mqtt.connects", "count", "Backend kết nối tới Mosquitto"],
      ["mqtt.disconnects", "count", "Backend mất kết nối Mosquitto"],
      ["mqtt.messages_received", "count", "Tổng bản tin MQTT nhận được"],
      ["engine.errors", "count", "Lỗi trong vòng chạy nền (tag loop)"],
    ]],
  ];
  // Metric gắn tag device_id -> link Datadog lọc sẵn theo thiết bị đang chọn
  const DEVICE_METRIC = /^(soil|air|flow|sensor|pump|mist|device|irrigation|water|fault)\./;
  let metricsExtra = { at: 0 };

  async function renderMetrics() {
    const box = $("#tab-metrics");
    const d = device();
    if (d && (Date.now() - metricsExtra.at > 15000 || metricsExtra.dev !== d._id)) {
      metricsExtra = { at: Date.now(), dev: d._id };
      try {
        const irr = await api(`devices/${encodeURIComponent(d._id)}/irrigation`);
        metricsExtra.irrBase = irr.baseline;
        metricsExtra.irr = (irr.events || []).find((e) => e.ratio_pct_per_l != null);
      } catch { /* chưa có lần tưới nào */ }
    }
    const app = S.meta?.datadog_app;
    const q = (S.metricQ || "").toLowerCase();
    const scope = d ? `device_id:${d._id}` : "";
    const link = (m) => (app
      ? `<a href="https://${app}/metric/explorer?exp_metric=${encodeURIComponent("agriguard." + m)}${DEVICE_METRIC.test(m) && scope ? `&exp_scope=${encodeURIComponent(scope)}` : ""}" target="_blank" rel="noopener">Datadog ↗</a>`
      : "");
    const total = METRIC_GROUPS.reduce((n, [, rows]) => n + rows.length, 0);
    const body = METRIC_GROUPS.map(([title, rows]) => {
      const shown = rows.filter(([m, , desc]) => !q || `agriguard.${m} ${desc}`.toLowerCase().includes(q));
      if (!shown.length) return "";
      return `<tr class="grp"><td colspan="5">${esc(title)}</td></tr>` + shown.map(([m, type, desc, val]) => {
        let v = "–";
        if (d && val) { try { v = val(d, metricsExtra); } catch { /* thiếu dữ liệu */ } }
        const cell = val ? esc(v) : '<span class="muted">xem trên Datadog</span>';
        return `<tr><td><code>agriguard.${esc(m)}</code></td><td>${type}</td><td>${esc(desc)}</td><td class="val">${cell}</td><td>${link(m)}</td></tr>`;
      }).join("");
    }).join("");
    const focused = document.activeElement?.id === "metric-q";
    box.innerHTML = `<div class="metrics-head">
        <p class="small muted">${total} metric AgriGuard gửi lên Datadog qua DogStatsD (tiền tố <code>agriguard.</code>, tag chung env/service; metric của thiết bị có thêm tag device_id, site, zone, source).
        Cột “Hiện tại” là giá trị mới nhất của <b>${esc(d?._id || "–")}</b>${app ? "; bấm “Datadog ↗” để mở Metrics Explorer lọc sẵn thiết bị này" : "; bật Datadog để có link mở Metrics Explorer"}.</p>
        <input id="metric-q" type="search" placeholder="Tìm metric… (vd. pump, zscore)" value="${esc(S.metricQ || "")}" aria-label="Tìm metric">
      </div>
      <div class="table-wrap"><table class="metrics"><thead><tr><th>Metric</th><th>Loại</th><th>Dùng để làm gì</th><th>Hiện tại</th><th></th></tr></thead>
      <tbody>${body || '<tr><td colspan="5" class="muted">Không có metric khớp</td></tr>'}</tbody></table></div>`;
    if (focused) { const i = $("#metric-q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
  }
  document.addEventListener("input", (ev) => {
    if (ev.target.id === "metric-q") { S.metricQ = ev.target.value; renderMetrics(); }
  });


  // ------------------------------------------------------------------ tab: AI tưới
  const RISK = { low: ["ok", "Thấp"], medium: ["warning", "Trung bình"], high: ["critical", "Cao"] };
  const TRIGGER = { change_stream: "Change Stream (lần tưới mới)", irrigation_event: "lần tưới mới", schedule: "định kỳ", manual: "bấm Học lại" };
  let aiCache = { at: 0 };

  function aiChart(f, hist, s) {
    const W = 760, H = 230, m = { l: 36, r: 10, t: 12, b: 24 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const pts = f ? f.points : [], plan = f ? f.with_plan : [];
    const t0 = hist.length ? new Date(hist[0].t).getTime() : (pts.length ? new Date(pts[0].t).getTime() : Date.now());
    const t1 = pts.length ? new Date(pts[pts.length - 1].t).getTime() : Date.now();
    const vals = [...hist.map((h) => h.soil), ...pts.map((p) => p.soil), ...plan.map((p) => p.soil), s.pump.soil_dry_pct, s.pump.soil_target_pct];
    const lo = Math.max(0, Math.floor(Math.min(...vals) / 10) * 10), hi = Math.min(100, Math.ceil(Math.max(...vals) / 10) * 10 || 100);
    const x = (t) => m.l + ((new Date(t).getTime() - t0) / Math.max(1, t1 - t0)) * iw;
    const y = (v) => m.t + ih - ((v - lo) / Math.max(1, hi - lo)) * ih;
    const path = (arr) => arr.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.soil).toFixed(1)}`).join("");
    let svg = `<svg viewBox="0 0 ${W} ${H}" class="water-svg" role="img" aria-label="Độ ẩm đất: 2 giờ qua và dự báo 6 giờ tới">`;
    for (let v = lo; v <= hi; v += 10) svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" class="ax">${v}</text>`;
    for (const [v, lab] of [[s.pump.soil_dry_pct, "khô"], [s.pump.soil_target_pct, "đủ ẩm"]]) svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--muted)" stroke-dasharray="3 4"/><text x="${W - m.r - 2}" y="${y(v) - 4}" text-anchor="end" class="ax">${lab} ${v}%</text>`;
    const now = pts.length ? pts[0].t : null;
    if (now) svg += `<line x1="${x(now)}" x2="${x(now)}" y1="${m.t}" y2="${m.t + ih}" stroke="var(--text-2)" stroke-width="1"/><text x="${x(now) + 4}" y="${m.t + 10}" class="ax">bây giờ</text>`;
    for (let k = 0; k <= 6; k++) { const t = t0 + ((t1 - t0) * k) / 6; svg += `<text x="${x(t)}" y="${H - 6}" text-anchor="${k === 0 ? "start" : k === 6 ? "end" : "middle"}" class="ax">${fmtTime.format(new Date(t)).slice(0, 5)}</text>`; }
    if (hist.length) svg += `<path d="${path(hist)}" fill="none" stroke="var(--series-soil)" stroke-width="2.2"/>`;
    if (pts.length) svg += `<path d="${path(pts)}" fill="none" stroke="var(--series-soil)" stroke-width="2" stroke-dasharray="6 5"/>`;
    const st = f?.plan?.start_at;
    if (st && f.plan.decision === "irrigate") {
      svg += `<path d="${path(plan)}" fill="none" stroke="var(--series-flow)" stroke-width="2.4"/>`;
      svg += `<line x1="${x(st)}" x2="${x(st)}" y1="${m.t}" y2="${m.t + ih}" stroke="var(--series-flow)" stroke-dasharray="2 3"/><text x="${x(st) + 4}" y="${m.t + ih - 6}" class="ax" style="fill:var(--series-flow)">tưới ${num(f.plan.liters, 2)} L</text>`;
    }
    return svg + "</svg>";
  }

  async function renderAi(force = false) {
    const box = $("#tab-ai");
    const d = device();
    if (!d) { box.innerHTML = '<p class="muted">Chưa có thiết bị.</p>'; return; }
    if (force || aiCache.dev !== d._id || Date.now() - aiCache.at > 30000) {
      if (aiCache.dev !== d._id) box.innerHTML = '<p class="muted small"><span class="spinner"></span> Đang tải dự báo AI…</p>';
      const data = await api(`devices/${encodeURIComponent(d._id)}/ai`);
      if (device()?._id !== d._id) return;
      aiCache = { dev: d._id, at: Date.now(), data };
    }
    const w = aiCache.data, s = S.settings, f = w.forecast, mdl = w.ai.model || {}, rel = w.ai.reliability || {};
    if (!s) return;
    const p = f?.plan || {}, rk = f?.risks || {};
    const card = (label, value, unit, sub, cls = "") => `<div class="kpi card ${cls}"><div class="label">${label}</div><div class="value">${value}${unit ? `<small>${unit}</small>` : ""}</div><div class="sub">${sub || ""}</div></div>`;
    const riskCard = (label, r, sub) => { const [c, t] = RISK[r?.level] || ["idle", "–"]; return card(label, `<span class="risk ${c}">${t}</span>`, "", `${r ? `điểm ${num(r.score, 2)} · ` : ""}${sub || ""}`); };
    const hm = (t) => (t ? fmtTime.format(new Date(t)).slice(0, 5) : "–");
    let next = "Chưa cần tưới", nextSub = p.dry_at ? `đất chạm ngưỡng khô lúc ${hm(p.dry_at)}` : "đất đủ ẩm trong 6 giờ tới";
    if (p.decision === "irrigate") { next = hm(p.start_at); nextSub = `${num(p.liters, 2)} L · bơm ${p.seconds} giây · đất lúc đó ${num(p.soil_at_start, 0)}%`; }
    if (p.decision === "learning") { next = "Đang học"; nextSub = "cần vài lần tưới để học %/L và lưu lượng bơm"; }
    const tc = p.threshold_compare;
    const cmp = tc && p.decision === "irrigate"
      ? `So với tưới theo ngưỡng (bật lúc ${hm(tc.start_at)}, ${num(tc.liters, 2)} L): nước bay hơi ước tính ${num(p.evap_loss_liters, 2)} L thay vì ${num(tc.evap_loss_liters, 2)} L, thời gian đất dưới ngưỡng khô ${p.stress_minutes_with} phút thay vì ${tc.stress_minutes} phút.`
      : "";
    const mode = w.mode;
    const weights = mdl.weights ? Object.entries(mdl.weights).map(([k, v]) => `<tr><td>${esc(AI_FEATURES[k] || k)}</td><td class="val">${v > 0 ? "+" : ""}${num(v, 3)}</td><td class="muted">${k.startsWith("hour_") ? "nhịp ngày – đêm (đọc cùng cặp sin/cos)" : v > 0 ? "tăng → đất khô nhanh hơn" : "tăng → đất khô chậm hơn"}</td></tr>`).join("") : "";
    box.innerHTML = `
      <p class="small muted">AI học tốc độ khô của đất từ dữ liệu thật của <b>${esc(d._id)}</b> (Ridge regression trên đặc trưng do MongoDB Aggregation Pipeline tạo),
        dự báo độ ẩm đất 6 giờ tới, rồi chọn thời điểm và lượng nước tối ưu. Bơm đang ở chế độ <b>${esc(mode || "–")}</b>${mode !== "AI" ? ` – <button data-mode="AI" data-act="pump">Chuyển bơm sang chế độ AI</button>` : " – AI đang điều khiển bơm"}.
        <span class="chip">${w.change_streams ? "Change Stream đang chạy: học lại ngay sau mỗi lần tưới" : "học lại sau mỗi lần tưới (không có Change Stream)"}</span></p>
      <div class="grid kpis water-kpis">
        ${card("Độ ẩm đất sau 1 giờ", num(f?.soil_1h, 1), "%", f ? `bây giờ ${num(f.soil_now, 1)}% · không tưới` : "chưa có dự báo")}
        ${card("Độ ẩm đất sau 6 giờ", num(f?.soil_6h, 1), "%", f ? (f.method === "ridge" ? "mô hình Ridge" : "tốc độ khô đo trực tiếp (chưa đủ dữ liệu học)") : "")}
        ${card("Lần tưới tới (AI)", next, "", nextSub)}
        ${riskCard("Nguy cơ thiếu nước", rk.water_shortage, rk.water_shortage && !rk.water_shortage.pump_available ? "bơm đang không tưới được" : "")}
        ${riskCard("Nguy cơ stress nhiệt", rk.heat_stress, rk.heat_stress?.temp_max != null ? `nhiệt độ cao nhất dự báo ${num(rk.heat_stress.temp_max, 1)}°C` : "")}
        ${card("Độ tin cậy (sai số thật 1 giờ)", rel.mae_1h_pct != null ? num(rel.mae_1h_pct, 2) : "–", rel.mae_1h_pct != null ? "%" : "", rel.n ? `MAE ${rel.n} lần đối chiếu / 24 giờ` : "chờ đối chiếu dự báo với thực tế sau 1 giờ")}
      </div>
      <div class="water-chart">
        <div class="water-legend small"><span><i class="ln" style="border-color:var(--series-soil)"></i>Độ ẩm đất 2 giờ qua</span><span><i class="ln dash" style="border-color:var(--series-soil)"></i>Dự báo nếu không tưới</span>
          <span><i class="ln" style="border-color:var(--series-flow)"></i>Dự báo theo lịch tưới AI</span>
          <button data-ai-train style="margin-left:auto">Học lại ngay</button></div>
        <div class="scroll">${aiChart(f, w.history || [], s)}</div>
        ${cmp ? `<p class="small">${cmp}</p>` : ""}
      </div>
      <div class="ai-cols">
        <div><h3>Mô hình đang dùng</h3>
          <p class="small">${mdl.status === "ok"
            ? `Ridge regression · ${mdl.n_samples} mẫu (học ${mdl.n_train}, kiểm tra ${mdl.n_test}) · MAE kiểm định <b>${num(mdl.mae_pct_per_hour, 2)} %/giờ</b> so với ${num(mdl.baseline_mae_pct_per_hour, 2)} nếu chỉ lấy trung bình (kỹ năng ${num(mdl.skill, 2)})`
            : `Chưa đủ dữ liệu để học (${mdl.n_samples ?? 0}/${s.ai.min_samples} mẫu khung 10 phút không tưới). Đang dùng tốc độ khô đo trực tiếp.`}
            · học lúc ${mdl.trained_at ? fmtDateTime.format(new Date(mdl.trained_at)) : "–"} (${esc(TRIGGER[mdl.trigger] || mdl.trigger || "–")})</p>
          ${weights ? `<div class="table-wrap"><table class="metrics"><thead><tr><th>Đặc trưng</th><th>Hệ số (chuẩn hoá)</th><th>Ý nghĩa</th></tr></thead><tbody>${weights}</tbody></table></div>` : ""}
        </div>
        <div><h3>Lịch sử học lại</h3><div class="table-wrap"><table class="metrics"><thead><tr><th>Lúc</th><th>Kích hoạt</th><th>Mẫu</th><th>MAE</th><th>Mốc TB</th></tr></thead><tbody>
          ${(w.models || []).map((x) => `<tr><td>${fmtDateTime.format(new Date(x.trained_at))}</td><td>${esc(TRIGGER[x.trigger] || x.trigger)}</td><td>${x.n_samples}</td><td class="val">${num(x.mae_pct_per_hour, 2)}</td><td class="val">${num(x.baseline_mae_pct_per_hour, 2)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">Chưa có</td></tr>'}
        </tbody></table></div>
          <h3>Dự báo vs thực tế (sau 1 giờ)</h3><div class="table-wrap"><table class="metrics"><thead><tr><th>Dự báo lúc</th><th>Dự báo</th><th>Thật</th><th>Sai số</th></tr></thead><tbody>
          ${(w.errors || []).slice(-6).reverse().map((e) => `<tr><td>${hm(e.created_at)}</td><td class="val">${num(e.soil_1h, 1)}%</td><td class="val">${num(e.actual_1h, 1)}%</td><td class="val">${e.error_1h > 0 ? "+" : ""}${num(e.error_1h, 1)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">Chờ đủ 1 giờ để đối chiếu</td></tr>'}
          </tbody></table></div></div>
      </div>
      <details class="pipeline"><summary class="small">Pipeline MongoDB tạo đặc trưng ML ($dateTrunc · $setWindowFields quá khứ & tương lai · $derivative · $exp tính VPD) và hồ sơ thời tiết theo giờ</summary>
        <pre>${esc(JSON.stringify(w.pipelines, null, 2))}</pre></details>`;
  }
  const AI_FEATURES = { temp: "Nhiệt độ", vpd: "VPD – độ khô không khí", soil: "Độ ẩm đất", hour_sin: "Giờ trong ngày (sin)", hour_cos: "Giờ trong ngày (cos)" };
  // ---- "Học lại ngay": cửa sổ hiển thị từng bước (pipeline MongoDB → đặc trưng → Ridge → lưu → dự báo lại)
  const COL = {
    ts: "Thời điểm", t: "Khung", soil_moisture_pct: "Đất %", air_temp_c: "Nhiệt °C", air_humidity_pct: "Ẩm KK %",
    pump_on: "Bơm", flow_lpm: "L/phút", soil: "Đất %", temp: "Nhiệt °C", hum: "Ẩm KK %", water: "Có tưới", mist: "Phun",
    soil_ma1h: "TB trượt 1 giờ", slope_past: "Dốc 30′ trước (%/h)", slope_next: "Dốc 60′ sau (%/h)", wet_past: "Tưới 60′ trước",
    wet_next: "Tưới 60′ sau", vpd: "VPD kPa", hour_sin: "Giờ sin", hour_cos: "Giờ cos", target: "Nhãn: khô %/giờ",
  };
  const fmtCell = (k, v) => {
    if (v == null) return "–";
    if (k === "ts" || k === "t") return fmtDateTime.format(new Date(v));
    if (typeof v === "boolean") return v ? "có" : "–";
    if ((k === "water" || k === "mist" || k === "wet_past" || k === "wet_next") && typeof v === "number") return v ? "có" : "–";
    return typeof v === "number" ? num(v, Math.abs(v) < 10 ? 3 : 1) : esc(v);
  };
  function stepData(s) {
    const d = s.data;
    if (!d) return "";
    let h = "";
    if (d.error) h += `<p class="step-err">Lỗi: ${esc(d.error)}</p>`;
    if (d.count != null) h += `<p><b class="step-count">${num(d.count, 0)}</b> ${esc(d.label || "")}${d.note ? ` · <span class="muted">${esc(d.note)}</span>` : ""}</p>`;
    else if (d.note) h += `<p class="muted">${esc(d.note)}</p>`;
    if (d.sample?.length) {
      const keys = Object.keys(d.sample[0]);
      h += `<div class="table-wrap"><table class="metrics step-table"><thead><tr>${keys.map((k) => `<th>${COL[k] || esc(k)}</th>`).join("")}</tr></thead>
        <tbody>${d.sample.map((r) => `<tr>${keys.map((k) => `<td class="val">${fmtCell(k, r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    }
    if (s.key === "train" && d.status === "ok") {
      h += `<p>${d.n_samples} mẫu (học ${d.n_train}, kiểm tra ${d.n_test}) · MAE <b>${num(d.mae_pct_per_hour, 3)} %/giờ</b> so với ${num(d.baseline_mae_pct_per_hour, 3)} nếu chỉ lấy trung bình · kỹ năng <b>${num(d.skill, 2)}</b></p>`;
      if (d.weights) h += `<p class="small">Hệ số: ${Object.entries(d.weights).map(([k, v]) => `${esc(AI_FEATURES[k] || k)} <b>${v > 0 ? "+" : ""}${num(v, 3)}</b>`).join(" · ")}</p>`;
    }
    if (s.key === "save" && d.model_id) h += `<p>Đã ghi <code>${esc(d.model_id)}</code> vào collection <code>${esc(d.collection)}</code></p>`;
    if (s.key === "plan" && d.soil_now != null) {
      h += `<p>Độ ẩm đất ${num(d.soil_now, 1)}% → sau 1 giờ <b>${num(d.soil_1h, 1)}%</b> → sau 6 giờ <b>${num(d.soil_6h, 1)}%</b> (${d.method === "ridge" ? "mô hình Ridge" : "tốc độ khô đo trực tiếp"})</p>
        <p>${d.decision === "irrigate" ? `Lịch tưới: <b>${num(d.liters, 2)} L</b> (${d.seconds} giây) lúc <b>${fmtTime.format(new Date(d.start_at)).slice(0, 5)}</b>` : d.decision === "learning" ? "Đang học %/L và lưu lượng – chưa lập lịch" : "Chưa cần tưới trong 6 giờ tới"}</p>`;
    }
    if (d.stages?.length) h += `<details><summary class="small">Stage MongoDB của bước này</summary><pre>${esc(JSON.stringify(d.stages, null, 2))}</pre></details>`;
    return h;
  }
  function trainModal(job) {
    let m = $("#train-modal");
    if (!m) {
      m = document.createElement("div");
      m.id = "train-modal"; m.className = "modal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-modal", "true");
      m.setAttribute("aria-labelledby", "train-title");
      document.body.appendChild(m);
    }
    m.hidden = false;
    m.innerHTML = `<div class="modal-box">
      <div class="modal-head"><h3 id="train-title">Học lại mô hình AI – ${esc(job.device_id)}</h3>
        <span class="chip" id="train-engine"></span><button class="modal-x" data-close-modal aria-label="Đóng">✕</button></div>
      <ol class="steps">${job.steps.map((s, i) => `<li class="step pending" data-step="${s.key}"></li>`).join("")}</ol>
      <div class="modal-foot"><span id="train-summary" class="small muted">Đang chạy…</span><button data-close-modal>Đóng</button></div></div>`;
    m._shown = {};
    updateTrainModal(job);
  }
  function updateTrainModal(job) {
    const m = $("#train-modal");
    if (!m || m.hidden) return;
    $("#train-engine").textContent = job.engine === "mongodb-aggregation" ? "chạy trên MongoDB Aggregation Pipeline" : job.engine ? "chế độ chạy thử (Python tương đương)" : "";
    job.steps.forEach((s, i) => {
      const sig = `${s.status}|${s.ms}`;
      if (m._shown[s.key] === sig) return;
      m._shown[s.key] = sig;
      const li = m.querySelector(`[data-step="${s.key}"]`);
      li.className = `step ${s.status}`;
      const icon = s.status === "done" ? "✓" : s.status === "error" ? "✕" : s.status === "running" ? '<span class="spinner"></span>' : i + 1;
      li.innerHTML = `<div class="step-head"><span class="step-icon">${icon}</span><b>${esc(s.title)}</b>${s.ms != null ? `<span class="muted small">${num(s.ms, 0)} ms</span>` : ""}</div>
        <div class="step-desc small muted">${esc(s.desc)}</div><div class="step-data">${stepData(s)}</div>`;
      if (s.status === "running") li.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    const sum = $("#train-summary");
    if (job.status === "done") {
      const r = job.result || {};
      sum.innerHTML = r.status === "ok" ? `<b>Xong.</b> Mô hình mới: ${r.n_samples} mẫu, MAE ${num(r.mae_pct_per_hour, 3)} %/giờ, kỹ năng ${num(r.skill, 2)}` : `<b>Xong.</b> Chưa đủ dữ liệu để học (${r.n_samples} mẫu)`;
      sum.className = "small";
    } else if (job.status === "error") { sum.textContent = "Có lỗi – xem bước được đánh dấu ✕"; sum.className = "small step-err"; }
  }
  document.addEventListener("click", async (ev) => {
    if (ev.target.closest("[data-close-modal]") || ev.target.id === "train-modal") { const m = $("#train-modal"); if (m) m.hidden = true; return; }
    const b = ev.target.closest("[data-ai-train]");
    if (!b || !device()) return;
    const dev = device()._id;
    b.disabled = true;
    try {
      let job = await post(`devices/${encodeURIComponent(dev)}/ai/train`);
      trainModal(job);
      while (job.status === "running") {
        await new Promise((r) => setTimeout(r, 400));
        job = await api(`devices/${encodeURIComponent(dev)}/ai/train/${job.job_id}`);
        updateTrainModal(job);
      }
      renderAi(true).catch(console.warn);
    } catch (e) { toast("Lỗi: " + e.message); }
    b.disabled = false;
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { const m = $("#train-modal"); if (m) m.hidden = true; } });

  // ------------------------------------------------------------------ tab: Báo cáo nước (MongoDB Aggregation)
  const FORECAST_METHOD = {
    usage_avg7: "trung bình nước thực dùng 7 ngày trước ($setWindowFields)",
    drying_rate: "tốc độ khô của đất ÷ phản hồi đất đã học (%/L) – chưa đủ 3 ngày dữ liệu tưới",
  };
  let waterCache = { at: 0 };

  function waterBars(rows) {
    const W = 760, H = 200, m = { l: 40, r: 8, t: 10, b: 26 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const max = Math.max(0.1, ...rows.map((r) => Math.max(r.liters, r.need_liters || 0, r.liters_avg7 || 0)));
    const bw = iw / rows.length;
    const y = (v) => m.t + ih - (v / max) * ih;
    const ticks = [0, max / 2, max];
    let svg = `<svg viewBox="0 0 ${W} ${H}" class="water-svg" role="img" aria-label="Lít nước theo ngày">`;
    ticks.forEach((v) => { svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" class="ax">${num(v, 1)}</text>`; });
    rows.forEach((r, i) => {
      const x = m.l + i * bw + bw * 0.18, w = bw * 0.64;
      const good = r.liters - r.wasted_liters;
      svg += `<g><title>${r.date}: ${num(r.liters, 2)} L, ${r.runs} lần tưới${r.wasted_liters ? `, ${num(r.wasted_liters, 2)} L không tới gốc` : ""}</title>`;
      if (good > 0) svg += `<rect x="${x}" y="${y(good)}" width="${w}" height="${y(0) - y(good)}" rx="2" fill="var(--series-hum)"/>`;
      if (r.wasted_liters > 0) svg += `<rect x="${x}" y="${y(r.liters)}" width="${w}" height="${y(good) - y(r.liters)}" rx="2" fill="var(--serious)"/>`;
      svg += `</g>`;
      if (rows.length <= 16 || i % 2 === 0) svg += `<text x="${x + w / 2}" y="${H - 8}" text-anchor="middle" class="ax">${r.date.slice(8)}/${r.date.slice(5, 7)}</text>`;
    });
    const line = (key, color, dash) => {
      const pts = rows.map((r, i) => (r[key] == null ? null : `${m.l + i * bw + bw / 2},${y(r[key])}`));
      let d = "", pen = false;
      pts.forEach((p) => { if (p) { d += (pen ? "L" : "M") + p; pen = true; } else pen = false; });
      return d ? `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" ${dash ? 'stroke-dasharray="5 4"' : ""}/>` : "";
    };
    svg += line("liters_avg7", "var(--text-2)", true) + line("need_liters", "var(--series-flow)", false);
    return svg + "</svg>";
  }

  async function renderWater(force = false) {
    const box = $("#tab-water");
    const d = device();
    if (!d) { box.innerHTML = '<p class="muted">Chưa có thiết bị.</p>'; return; }
    const tank = Number(store.get("agri.tank")) || null;
    const key = `${d._id}|${S.waterDays || 14}|${tank}`;
    if (force || waterCache.key !== key || Date.now() - waterCache.at > 30000) {
      if (!waterCache.data || waterCache.key !== key) box.innerHTML = '<p class="muted small"><span class="spinner"></span> Đang chạy aggregation pipeline…</p>';
      const q = `days=${S.waterDays || 14}${tank ? `&tank_liters=${tank}` : ""}`;
      const data = await api(`devices/${encodeURIComponent(d._id)}/water/daily?${q}`);
      if (device()?._id !== d._id) return;          // đã chọn thiết bị khác trong lúc chờ -> bỏ kết quả cũ
      waterCache = { key, at: Date.now(), data };
    }
    const w = waterCache.data, s = w.summary, rows = w.rows;
    const card = (label, value, unit, sub) => `<div class="kpi card"><div class="label">${label}</div><div class="value">${value}${unit ? `<small>${unit}</small>` : ""}</div><div class="sub">${sub || ""}</div></div>`;
    const engine = w.engine === "mongodb-aggregation"
      ? "tính bằng <b>MongoDB Aggregation Pipeline</b>"
      : "chế độ chạy thử (mongomock) – tính bằng Python tương đương pipeline";
    box.innerHTML = `
      <p class="small muted">Nhà màng cần biết mỗi ngày dùng bao nhiêu nước, bao nhiêu nước không tới gốc và bể trữ còn đủ mấy ngày (mùa khô, xâm nhập mặn).
        Báo cáo của <b>${esc(d._id)}</b>, ${engine} trên <code>irrigation_events</code> và <code>telemetry</code> (time series).</p>
      <div class="grid kpis water-kpis">
        ${card("Dự báo nước ngày mai", num(s.forecast_liters_per_day, 2), "L", s.forecast_method ? FORECAST_METHOD[s.forecast_method] : "chưa đủ dữ liệu để dự báo")}
        ${card("Nước cần 7 ngày tới", num(s.forecast_liters_7d, 1), "L", "theo dự báo ở bên trái")}
        <div class="kpi card"><div class="label">Bể trữ đủ dùng</div><div class="value">${s.tank_days != null ? num(s.tank_days, 1) : "–"}<small>ngày</small></div>
          <div class="sub"><label>Dung tích bể <input id="tank-l" type="number" min="1" step="1" value="${tank || ""}" placeholder="lít" style="width:90px"> L</label></div></div>
        ${card("7 ngày qua", num(s.liters_7d, 2), "L", `${s.runs_7d} lần tưới · ${s.bad_runs_7d} lần bất thường`)}
        ${card("Nước không tới gốc (7 ngày)", num(s.wasted_liters_7d, 2), "L", s.wasted_pct_7d != null ? `${num(s.wasted_pct_7d, 1)}% tổng nước tưới – lần tưới có z-score âm` : "lần tưới có z-score âm")}
        ${card("Tốc độ khô của đất (3 ngày)", num(s.drying_pct_per_hour_3d, 2), "%/giờ", s.baseline_pct_per_l != null ? `phản hồi đất đã học ${num(s.baseline_pct_per_l, 2)} %/L` : "chưa học xong phản hồi đất")}
      </div>
      <div class="water-chart">
        <div class="water-legend small"><span><i style="background:var(--series-hum)"></i>Nước tới gốc</span><span><i style="background:var(--serious)"></i>Nước không tới gốc</span>
          <span><i class="ln dash"></i>Trung bình 7 ngày trước</span><span><i class="ln" style="border-color:var(--series-flow)"></i>Nước cây cần (ước tính từ tốc độ khô)</span>
          <span class="seg" id="water-days">${[7, 14, 30].map((n) => `<button data-wdays="${n}" class="${(S.waterDays || 14) === n ? "on" : ""}">${n} ngày</button>`).join("")}</span></div>
        <div class="scroll">${waterBars(rows)}</div>
      </div>
      <div class="table-wrap"><table class="metrics water"><thead><tr><th>Ngày</th><th>Lần tưới</th><th>Bất thường</th><th>Nước (L)</th><th>Không tới gốc (L)</th>
        <th>TB 7 ngày trước (L)</th><th>Luỹ kế (L)</th><th>Bơm (phút)</th><th>Tốc độ khô (%/giờ)</th><th>Cây cần (L, ước tính)</th><th>Nhiệt TB / max (°C)</th><th>Đất thấp nhất (%)</th></tr></thead>
        <tbody>${rows.slice().reverse().map((r) => `<tr class="${r.bad_runs ? "warn" : ""}"><td>${r.date.slice(8)}/${r.date.slice(5, 7)}</td><td>${r.runs}</td><td>${r.bad_runs || ""}</td>
          <td class="val">${num(r.liters, 2)}</td><td class="val">${r.wasted_liters ? num(r.wasted_liters, 2) : ""}</td><td class="val">${num(r.liters_avg7, 2)}</td>
          <td class="val">${num(r.liters_cumulative, 2)}</td><td class="val">${num(r.pump_minutes, 1)}</td><td class="val">${num(r.drying_pct_per_hour, 2)}</td>
          <td class="val">${num(r.need_liters, 2)}</td><td class="val">${num(r.temp_avg, 1)} / ${num(r.temp_max, 1)}</td><td class="val">${num(r.soil_min, 0)}</td></tr>`).join("")}</tbody></table></div>
      <details class="pipeline"><summary class="small">Xem 2 pipeline MongoDB đã chạy ($dateTrunc · $densify · $fill · $setWindowFields · $derivative)</summary>
        <p class="small muted">1) <code>irrigation_events</code> → số lần tưới, lít, lít không tới gốc theo ngày; trung bình 7 ngày trước, xu hướng, luỹ kế.
        2) <code>telemetry</code> → độ dốc độ ẩm đất (%/giờ) mỗi 10 phút, bỏ 60 phút sau khi tưới, gom theo ngày.</p>
        <pre>${esc(JSON.stringify(w.pipelines, null, 2))}</pre></details>`;
  }
  document.addEventListener("change", (ev) => {
    if (ev.target.id === "tank-l") { store.set("agri.tank", ev.target.value || ""); renderWater(true).catch(console.warn); }
  });
  document.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-wdays]");
    if (b) { S.waterDays = Number(b.dataset.wdays); renderWater(true).catch(console.warn); }
  });

  // ------------------------------------------------------------------ data loops
  async function refresh() {
    try {
      const [devices, incidents] = await Promise.all([api("devices"), api("incidents?status=active")]);
      S.devices = devices; S.incidents = incidents;
      if (!S.selected || !devices.some((d) => d._id === S.selected)) S.selected = devices[0]?._id || null;
      if (S.selected) S.commands = await api(`commands?device_id=${encodeURIComponent(S.selected)}&limit=25`);
      renderTabs(); renderBanner(); renderKpis(); renderActuator("pump"); renderActuator("mist"); renderIncidents();
      if (S.tab === "commands") renderCommands();
      if (S.tab === "metrics") renderMetrics();
      if (S.tab === "water" && document.activeElement?.id !== "tank-l" && Date.now() - waterCache.at > 60000) renderWater().catch(console.warn);
      if (S.tab === "ai" && Date.now() - aiCache.at > 30000) renderAi().catch(console.warn);
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
        if (S.tab === "water") $("#tab-water").innerHTML = '<p class="muted small"><span class="spinner"></span> Đang chạy aggregation pipeline…</p>';
        await refresh(); loadCharts(); if (S.tab === "sim") renderSim();
        if (S.tab === "water") renderWater(true).catch(console.warn);
        if (S.tab === "ai") { $("#tab-ai").innerHTML = '<p class="muted small"><span class="spinner"></span> Đang tải dự báo AI…</p>'; renderAi(true).catch(console.warn); }
      } else if (b.dataset.min) {
        S.range = +b.dataset.min;
        document.querySelectorAll("#range button").forEach((x) => x.classList.toggle("on", x === b));
        loadCharts();
      } else if (b.dataset.tab) {
        S.tab = b.dataset.tab; renderTab();
      } else if (b.dataset.mode && d) {
        await put(`devices/${encodeURIComponent(d._id)}/actuators/${b.dataset.act}/mode`, { mode: b.dataset.mode });
        toast(`Đã chuyển chế độ ${b.dataset.act === "pump" ? "bơm" : "phun sương"}: ${b.textContent}`); refresh();
        if (S.tab === "ai") renderAi(true).catch(console.warn);
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
    new FormData(ev.target).forEach((v, k) => { const [g, f] = k.split("."); (body[g] ||= {})[f] = f === "detect_high_response" ? v === "1" : Number(v); });
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
