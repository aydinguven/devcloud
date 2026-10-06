// Dependency-free SVG charts for GenAI usage (air-gapped installs have no CDN).
(() => {
  const NS = "http://www.w3.org/2000/svg";
  const PALETTE = ["#d50032", "#2563eb", "#0f766e", "#f59e0b", "#7c3aed", "#0891b2", "#db2777", "#65a30d", "#ea580c", "#475569"];

  const compact = (value) => {
    const number = Number(value) || 0;
    const abs = Math.abs(number);
    if (abs >= 1e9) return `${(number / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${(number / 1e3).toFixed(1)}K`;
    return Number.isInteger(number) ? String(number) : number.toFixed(2);
  };
  const money = (value) => `$${(Number(value) || 0).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  const formatter = (metric) => (metric === "spend" ? money : compact);

  function el(name, attrs = {}, parent) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (parent) parent.append(node);
    return node;
  }

  let tooltip;
  function showTip(event, html) {
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "genai-tooltip";
      document.body.append(tooltip);
    }
    tooltip.replaceChildren(...html.map((line, index) => {
      const row = document.createElement(index ? "div" : "strong");
      row.textContent = line;
      return row;
    }));
    tooltip.hidden = false;
    const x = Math.min(event.pageX + 14, window.scrollX + window.innerWidth - tooltip.offsetWidth - 8);
    tooltip.style.left = `${x}px`;
    tooltip.style.top = `${event.pageY - tooltip.offsetHeight - 12}px`;
  }
  const hideTip = () => { if (tooltip) tooltip.hidden = true; };

  function niceMax(value) {
    if (value <= 0) return 1;
    const power = 10 ** Math.floor(Math.log10(value));
    const step = [1, 2, 2.5, 5, 10].find((m) => m * power >= value) || 10;
    return step * power;
  }

  // Stacked/grouped bar chart with an optional overlay line (own axis).
  function bars(container, {labels, series, line, metric = "total_tokens", height = 260}) {
    const width = Math.max(container.clientWidth || 720, 320);
    const pad = {top: 16, right: line ? 48 : 12, bottom: 28, left: 52};
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const totals = labels.map((_, i) => series.reduce((sum, s) => sum + (s.values[i] || 0), 0));
    const max = niceMax(Math.max(0, ...totals));
    const fmt = formatter(metric);
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width: "100%", height, class: "genai-svg"});

    for (let i = 0; i <= 4; i += 1) {
      const y = pad.top + plotH - (plotH * i) / 4;
      el("line", {x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: "genai-grid-line"}, svg);
      el("text", {x: pad.left - 8, y: y + 4, "text-anchor": "end", class: "genai-axis"}, svg).textContent = fmt((max * i) / 4);
    }
    const slot = plotW / labels.length;
    const barW = Math.max(2, Math.min(28, slot * 0.68));
    labels.forEach((label, i) => {
      const x = pad.left + slot * i + (slot - barW) / 2;
      let base = pad.top + plotH;
      series.forEach((s, sIndex) => {
        const value = s.values[i] || 0;
        const h = (value / max) * plotH;
        if (h > 0) {
          const radius = sIndex === series.length - 1 ? Math.min(4, barW / 2) : 0;
          el("rect", {x, y: base - h, width: barW, height: h, rx: radius, fill: s.color, class: "genai-bar"}, svg);
        }
        base -= h;
      });
      const hit = el("rect", {x: pad.left + slot * i, y: pad.top, width: slot, height: plotH, fill: "transparent"}, svg);
      hit.addEventListener("mousemove", (event) => showTip(event, [
        label,
        ...series.filter((s) => s.values[i]).map((s) => `${s.name}: ${fmt(s.values[i])}`),
        ...(line ? [`${line.name}: ${formatter(line.metric)(line.values[i])}`] : []),
      ]));
      hit.addEventListener("mouseleave", hideTip);
      const every = Math.ceil(labels.length / Math.max(1, Math.floor(plotW / 64)));
      if (i % every === 0) {
        el("text", {x: pad.left + slot * i + slot / 2, y: height - 8, "text-anchor": "middle", class: "genai-axis"}, svg)
          .textContent = label.slice(5);
      }
    });

    if (line) {
      const lineMax = niceMax(Math.max(0, ...line.values));
      const points = line.values.map((value, i) => [
        pad.left + slot * i + slot / 2,
        pad.top + plotH - ((value || 0) / lineMax) * plotH,
      ]);
      const path = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
      el("path", {d: path, fill: "none", stroke: line.color, "stroke-width": 2.5, class: "genai-line"}, svg);
      points.forEach(([x, y]) => el("circle", {cx: x, cy: y, r: 2.5, fill: line.color}, svg));
      el("text", {x: width - pad.right + 8, y: pad.top + 4, class: "genai-axis"}, svg).textContent = formatter(line.metric)(lineMax);
    }
    container.replaceChildren(svg);
  }

  function donut(container, items, {metric = "total_tokens", size = 200} = {}) {
    const sum = items.reduce((acc, item) => acc + item.value, 0);
    const total = sum || 1;
    const svg = el("svg", {viewBox: `0 0 ${size} ${size}`, width: size, height: size, class: "genai-svg"});
    const r = size / 2 - 14;
    const c = 2 * Math.PI * r;
    let offset = 0;
    items.forEach((item, index) => {
      const length = (item.value / total) * c;
      const arc = el("circle", {
        cx: size / 2, cy: size / 2, r, fill: "none", stroke: item.color || PALETTE[index % PALETTE.length],
        "stroke-width": 24, "stroke-dasharray": `${length} ${c - length}`, "stroke-dashoffset": -offset,
        transform: `rotate(-90 ${size / 2} ${size / 2})`, class: "genai-arc",
      }, svg);
      arc.addEventListener("mousemove", (event) => showTip(event, [item.label, `${formatter(metric)(item.value)} · %${((item.value / total) * 100).toFixed(1)}`]));
      arc.addEventListener("mouseleave", hideTip);
      offset += length;
    });
    el("text", {x: size / 2, y: size / 2 - 2, "text-anchor": "middle", class: "genai-donut-total"}, svg).textContent = formatter(metric)(sum);
    el("text", {x: size / 2, y: size / 2 + 18, "text-anchor": "middle", class: "genai-axis"}, svg).textContent = "toplam";
    container.replaceChildren(svg);
  }

  function sparkline(values, {width = 120, height = 28, color = "#d50032"} = {}) {
    const svg = el("svg", {viewBox: `0 0 ${width} ${height}`, width, height, class: "genai-spark"});
    const max = Math.max(1, ...values);
    const step = width / Math.max(1, values.length - 1);
    const points = values.map((value, i) => `${(i * step).toFixed(1)},${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`);
    el("polygon", {points: `0,${height} ${points.join(" ")} ${width},${height}`, fill: color, opacity: 0.12}, svg);
    el("polyline", {points: points.join(" "), fill: "none", stroke: color, "stroke-width": 1.6}, svg);
    return svg;
  }

  window.GenAiCharts = {PALETTE, compact, money, formatter, bars, donut, sparkline};
})();
