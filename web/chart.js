/* Biểu đồ đường SVG gọn nhẹ (không cần thư viện/CDN): một chuỗi số liệu, vùng tô trạng thái,
   đường ngưỡng, trục thời gian theo giờ Việt Nam, crosshair + tooltip khi rê chuột. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const TZ = "Asia/Ho_Chi_Minh";
  const fmtHM = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtHMS = new Intl.DateTimeFormat("vi-VN", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function niceTicks(min, max, count) {
    const span = max - min || 1;
    const step0 = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(6));
    return { lo, hi, ticks };
  }

  function render(host, opt) {
    host.innerHTML = "";
    const pts = (opt.points || []).filter(p => p.t != null);
    const vals = pts.map(p => p[opt.key]).filter(v => v != null);
    if (!vals.length) {
      host.innerHTML = '<div class="empty">Chưa có dữ liệu</div>';
      return;
    }
    const W = host.clientWidth || 400, H = host.clientHeight || 190;
    const m = { l: 38, r: 10, t: 8, b: 22 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    let ymin = Math.min(...vals), ymax = Math.max(...vals);
    (opt.thresholds || []).forEach(t => { ymin = Math.min(ymin, t.v); ymax = Math.max(ymax, t.v); });
    if (opt.yMin != null) ymin = Math.min(ymin, opt.yMin);
    if (opt.yMax != null) ymax = Math.max(ymax, opt.yMax);
    if (ymax - ymin < (opt.minSpan || 1)) { const c = (ymax + ymin) / 2; ymin = c - (opt.minSpan || 1) / 2; ymax = c + (opt.minSpan || 1) / 2; }
    const yt = niceTicks(ymin, ymax, 4);
    if (opt.floor != null) yt.lo = Math.max(yt.lo, opt.floor);

    const t0 = opt.from || pts[0].t, t1 = opt.to || pts[pts.length - 1].t;
    const x = t => m.l + ((t - t0) / Math.max(1, t1 - t0)) * iw;
    const y = v => m.t + ih - ((v - yt.lo) / (yt.hi - yt.lo || 1)) * ih;

    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": opt.label || "" }, host);

    // grid + y axis
    const ax = el("g", { class: "axis" }, svg);
    yt.ticks.filter(v => v >= yt.lo && v <= yt.hi).forEach(v => {
      el("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, ax);
      const tx = el("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, ax);
      tx.textContent = (+v.toFixed(2)).toLocaleString("vi-VN");
    });
    // x axis ticks
    const nx = Math.max(2, Math.min(6, Math.floor(iw / 90)));
    for (let i = 0; i <= nx; i++) {
      const t = t0 + (i / nx) * (t1 - t0);
      const tx = el("text", { x: x(t), y: H - 5, "text-anchor": i === 0 ? "start" : i === nx ? "end" : "middle" }, ax);
      tx.textContent = fmtHM.format(new Date(t));
    }

    // state band (bơm / phun sương đang chạy)
    if (opt.bandKey) {
      const step = opt.stepMs || 5000;
      let start = null;
      pts.forEach((p, i) => {
        const on = p[opt.bandKey] === 1;
        const next = pts[i + 1];
        if (on && start === null) start = p.t;
        if (start !== null && (!on || !next)) {
          const end = on ? p.t + step : p.t;
          el("rect", { x: x(start), y: m.t, width: Math.max(2, x(end) - x(start)), height: ih, fill: opt.bandColor || "var(--band)" }, svg);
          start = null;
        }
      });
    }

    // vùng mất kết nối (từ sự cố device_offline – không phụ thuộc bước gom dữ liệu)
    const offline = (opt.offline || []).filter(o => o.to >= t0 && o.from <= t1);
    offline.forEach(o => {
      const xa = x(Math.max(o.from, t0)), xb = x(Math.min(o.to, t1));
      el("rect", { class: "offline-band", x: xa, y: m.t, width: Math.max(3, xb - xa), height: ih }, svg);
      if (xb - xa >= 52) {
        const lab = el("text", { class: "offline-label", x: (xa + xb) / 2, y: m.t + 12, "text-anchor": "middle" }, svg);
        lab.textContent = "mất kết nối";
      }
    });
    const crossesOffline = (a, b) => offline.some(o => o.from < b && o.to > a);

    // thresholds
    (opt.thresholds || []).forEach(t => {
      el("line", { class: "threshold", x1: m.l, x2: W - m.r, y1: y(t.v), y2: y(t.v) }, svg);
      const lab = el("text", { class: "threshold-label", x: W - m.r - 2, y: y(t.v) - 3, "text-anchor": "end" }, svg);
      lab.textContent = t.label;
    });

    // line (ngắt khi thiếu dữ liệu hoặc mất kết nối)
    const gap = (opt.stepMs || 5000) * 3.5;
    let d = "", prev = null;
    pts.forEach(p => {
      const v = p[opt.key];
      if (v == null) { prev = null; return; }
      const cmd = prev && p.t - prev.t <= gap && !crossesOffline(prev.t, p.t) ? "L" : "M";
      d += `${cmd}${x(p.t).toFixed(1)},${y(v).toFixed(1)}`;
      prev = p;
    });
    el("path", { d, fill: "none", stroke: opt.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);

    // last value marker
    const last = [...pts].reverse().find(p => p[opt.key] != null);
    if (last) el("circle", { cx: x(last.t), cy: y(last[opt.key]), r: 4, fill: opt.color, stroke: "var(--surface)", "stroke-width": 2 }, svg);

    // hover layer
    const hover = el("g", { style: "display:none" }, svg);
    const vline = el("line", { class: "crosshair", y1: m.t, y2: m.t + ih }, hover);
    const dot = el("circle", { r: 4.5, fill: opt.color, stroke: "var(--surface)", "stroke-width": 2 }, hover);
    const hit = el("rect", { x: m.l, y: 0, width: iw, height: H, fill: "transparent" }, svg);
    const tip = document.getElementById("tooltip");
    hit.addEventListener("pointermove", ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      const tt = t0 + ((px - m.l) / iw) * (t1 - t0);
      const off = offline.find(o => tt >= o.from && tt <= o.to);
      if (off) {
        hover.style.display = "none";
        const secs = Math.round((off.to - off.from) / 1000);
        tip.textContent = `Mất kết nối ${fmtHMS.format(new Date(off.from))}–${off.ongoing ? "đang mất" : fmtHMS.format(new Date(off.to))} (${secs} giây)`;
        tip.hidden = false;
        tip.style.left = Math.min(window.innerWidth - tip.offsetWidth - 8, ev.clientX + 12) + "px";
        tip.style.top = ev.clientY - 36 + "px";
        return;
      }
      let best = null, bd = Infinity;
      pts.forEach(p => { const dd = Math.abs(p.t - tt); if (dd < bd && p[opt.key] != null) { bd = dd; best = p; } });
      if (!best) return;
      hover.style.display = "";
      vline.setAttribute("x1", x(best.t)); vline.setAttribute("x2", x(best.t));
      dot.setAttribute("cx", x(best.t)); dot.setAttribute("cy", y(best[opt.key]));
      const val = best[opt.key].toLocaleString("vi-VN", { maximumFractionDigits: opt.decimals ?? 1 });
      const band = opt.bandKey ? ` · ${opt.bandLabel}: ${best[opt.bandKey] ? "đang chạy" : "tắt"}` : "";
      tip.textContent = `${fmtHMS.format(new Date(best.t))} · ${val} ${opt.unit}${band}`;
      tip.hidden = false;
      const tw = tip.offsetWidth;
      tip.style.left = Math.min(window.innerWidth - tw - 8, ev.clientX + 12) + "px";
      tip.style.top = ev.clientY - 36 + "px";
    });
    hit.addEventListener("pointerleave", () => { hover.style.display = "none"; tip.hidden = true; });
  }

  const state = new Map();
  window.LineChart = {
    draw(host, opt) {
      state.set(host, opt);
      render(host, opt);
      if (!host._ro && "ResizeObserver" in window) {
        let w = host.clientWidth;
        host._ro = new ResizeObserver(() => {
          if (Math.abs(host.clientWidth - w) > 4) { w = host.clientWidth; render(host, state.get(host)); }
        });
        host._ro.observe(host);
      }
    },
  };
})();
