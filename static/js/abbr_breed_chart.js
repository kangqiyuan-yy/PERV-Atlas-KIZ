// Shared breed × γ/β stacked bar chart (overview + browser).
(function (global) {
  'use strict';

  function cssColor(varName, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
    return v || fallback;
  }

  function readPalette() {
    return {
      gamma: cssColor('--ov-gamma', '#2563eb'),
      beta: cssColor('--ov-beta', '#f59e0b'),
      east: cssColor('--ov-east', '#10b981'),
      west: cssColor('--ov-west', '#ef4444'),
      wild: cssColor('--ov-wild', '#64748b'),
    };
  }

  const chartInstances = new Set();

  function barAnimExtra() {
    return (global.ChartAnim && global.ChartAnim.BAR_ANIM) || {};
  }

  function reflowChart(chart) {
    if (!chart || !chart.getDom) return;
    const el = chart.getDom();
    el.style.width = '';
    el.style.maxWidth = '';
    chart.resize();
    if (chart.__abbrPinnedIndex != null) {
      positionPinCard(chart, chart.__abbrPinnedIndex);
    }
  }

  if (!global.__abbrBreedResizeHook) {
    global.__abbrBreedResizeHook = true;
    window.addEventListener('resize', () => {
      chartInstances.forEach(reflowChart);
    });
  }

  if (!global.__abbrPinEscBound) {
    global.__abbrPinEscBound = true;
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      chartInstances.forEach((c) => {
        if (c.__abbrPinnedIndex != null) hidePinCard(c);
      });
    });
  }

  function groupLabelRichKey(group) {
    if (group === 'Eastern') return 'east';
    if (group === 'Western') return 'west';
    if (group === 'Wild') return 'wild';
    return 'other';
  }

  function resolveChart(target) {
    if (!target || !global.echarts) return null;
    if (target.setOption) return target;
    const el = typeof target === 'string' ? document.getElementById(target) : target;
    if (!el) return null;
    return global.echarts.getInstanceByDom(el) || global.echarts.init(el);
  }

  function escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function escAttr(s) {
    return escHtml(s);
  }

  /** Line series used only for legend swatches — must NOT be type bar (that splits bar groups). */
  function legendOnlyLineSeries(name, color, n) {
    return {
      name,
      type: 'line',
      xAxisIndex: 0,
      data: Array(n).fill(null),
      itemStyle: { color },
      lineStyle: { width: 0, opacity: 0 },
      symbol: 'roundRect',
      symbolSize: [14, 10],
      showSymbol: false,
      silent: true,
      tooltip: { show: false },
      legendHoverLink: false,
      emphasis: { disabled: true },
      animation: false,
      z: -10,
    };
  }

  function ensurePinCard(chart) {
    const dom = chart.getDom();
    let card = dom.querySelector('.abbr-pin-card');
    if (card) return card;
    card = document.createElement('div');
    card.className = 'abbr-pin-card';
    card.hidden = true;
    card.setAttribute('role', 'dialog');
    card.addEventListener('click', (e) => e.stopPropagation());
    dom.appendChild(card);
    return card;
  }

  function setHoverTooltip(chart, enabled) {
    try {
      chart.setOption({ tooltip: { show: !!enabled } });
      if (!enabled && typeof chart.dispatchAction === 'function') {
        chart.dispatchAction({ type: 'hideTip' });
      }
    } catch (_) { /* ignore */ }
  }

  function hidePinCard(chart) {
    chart.__abbrPinnedIndex = null;
    chart.__abbrPinnedName = null;
    const card = chart.getDom().querySelector('.abbr-pin-card');
    if (card) card.hidden = true;
    setHoverTooltip(chart, true);
  }

  function positionPinCard(chart, idx) {
    const list = chart.__abbrItems || [];
    const it = list[idx];
    const card = ensurePinCard(chart);
    if (!it) {
      card.hidden = true;
      return;
    }
    const dom = chart.getDom();
    let x = 80;
    let y = 56;
    try {
      const pt = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [idx, it.count || 0]);
      if (pt && pt.length === 2) {
        x = pt[0];
        y = pt[1];
      }
    } catch (_) { /* keep defaults */ }

    card.hidden = false;
    const w = card.offsetWidth || 200;
    const h = card.offsetHeight || 140;
    const left = Math.min(Math.max(x - w / 2, 8), Math.max(8, dom.clientWidth - w - 8));
    const top = Math.min(Math.max(y - h - 10, 36), Math.max(36, dom.clientHeight - h - 8));
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }

  function showPinCard(chart, idx, gammaName, betaName) {
    const list = chart.__abbrItems || [];
    const it = list[idx];
    if (!it) return;
    const card = ensurePinCard(chart);
    const g = it.gamma_count != null ? it.gamma_count : 0;
    const b = it.beta_count != null ? it.beta_count : 0;
    const grp = it.group
      ? `<div class="abbr-pin-row">Group: ${escHtml(it.group)}</div>`
      : '';
    let asmLine = '';
    if (it.assembly) {
      if (it.url) {
        asmLine = `<div class="abbr-pin-row"><a class="abbr-pin-asm" href="${escAttr(it.url)}" `
          + `target="_blank" rel="noopener noreferrer">${escHtml(it.assembly)}</a></div>`;
      } else {
        asmLine = `<div class="abbr-pin-row"><span class="abbr-pin-asm-plain">${escHtml(it.assembly)}</span></div>`;
      }
    }
    card.innerHTML = ''
      + '<button type="button" class="abbr-pin-close" aria-label="Close">&times;</button>'
      + `<div class="abbr-pin-title">${escHtml(it.full_name || it.name)} (${escHtml(it.name)})</div>`
      + grp
      + `<div class="abbr-pin-row">${escHtml(gammaName)}: ${g}</div>`
      + `<div class="abbr-pin-row">${escHtml(betaName)}: ${b}</div>`
      + `<div class="abbr-pin-row">Total: ${it.count}</div>`
      + asmLine
      + '<div class="abbr-pin-hint">Click bar again or press Esc to unpin</div>';

    const close = card.querySelector('.abbr-pin-close');
    if (close) {
      close.addEventListener('click', (e) => {
        e.stopPropagation();
        hidePinCard(chart);
      });
    }
    chart.__abbrPinnedIndex = idx;
    chart.__abbrPinnedName = it.name;
    positionPinCard(chart, idx);
    // Hide axis tooltip while pinned — same content would otherwise overlap.
    setHoverTooltip(chart, false);
  }

  function renderAbbrBreedChart(target, items) {
    const chart = resolveChart(target);
    if (!chart) return null;
    const list = items || [];
    const PALETTE = readPalette();
    const gammaName = 'γ.ERV';
    const betaName = 'β.ERV';
    const n = list.length;

    chart.__abbrItems = list;
    chartInstances.add(chart);

    const categories = list.map((x) => x.name);

    // Only show legend entries that actually have data in this dataset.
    const hasGamma = list.some((x) => (x.gamma_count || 0) > 0);
    const hasBeta = list.some((x) => (x.beta_count || 0) > 0);
    // Group legend mirrors the colored x-axis labels, so include every group
    // present among the breeds (zero-count breeds still appear on the axis).
    const presentGroups = new Set(list.map((x) => x.group));
    const ervLegendData = [];
    if (hasGamma) ervLegendData.push({ name: gammaName, itemStyle: { color: PALETTE.gamma } });
    if (hasBeta) ervLegendData.push({ name: betaName, itemStyle: { color: PALETTE.beta } });
    const groupLegendDefs = [
      { name: 'Eastern', color: PALETTE.east },
      { name: 'Western', color: PALETTE.west },
      { name: 'Wild', color: PALETTE.wild },
    ].filter((d) => presentGroups.has(d.name));
    const groupLegendData = groupLegendDefs.map((d) => ({ name: d.name, itemStyle: { color: d.color } }));

    const titleStyle = { fontSize: 11, color: '#64748b', fontWeight: 600 };

    // Mutable state for label formatters — updated by legendselectchanged
    const labelSel = { gamma: true, beta: true };

    // γ label: total on γ when β segment is absent; otherwise β carries the total.
    function gammaLabel(p) {
      const it = list[p.dataIndex];
      if (!it) return '';
      const beta = it.beta_count != null ? it.beta_count : 0;
      const g = it.gamma_count != null ? it.gamma_count : it.count;
      if (labelSel.gamma && labelSel.beta) {
        if (beta > 0) return '';
        return String(it.count);
      }
      if (labelSel.gamma) return String(g);
      return '';
    }

    // β label: total on stack top when both visible and β > 0; else β count only.
    function betaLabel(p) {
      const it = list[p.dataIndex];
      if (!it) return '';
      const beta = it.beta_count != null ? it.beta_count : 0;
      if (!labelSel.beta) return '';
      if (labelSel.gamma && labelSel.beta) {
        return beta > 0 ? String(it.count) : '';
      }
      return String(beta);
    }

    const zeroLabelSeries = {
      name: '__zero_labels',
      type: 'scatter',
      xAxisIndex: 0,
      yAxisIndex: 0,
      symbolSize: 0,
      silent: true,
      tooltip: { show: false },
      legendHoverLink: false,
      animation: false,
      data: list.filter((x) => x.count === 0).map((x) => [x.name, 0]),
      label: {
        show: true,
        formatter: '0',
        position: 'top',
        fontSize: 9,
        color: '#94a3b8',
      },
      z: 10,
    };

    chart.setOption({
      ...barAnimExtra(),
      grid: { left: 48, right: 16, top: 54, bottom: 8, containLabel: true },
      title: [
        {
          text: 'ERV Type',
          left: '18%',
          top: 5,
          textStyle: titleStyle,
        },
        {
          text: 'Group',
          left: '54%',
          top: 5,
          textStyle: titleStyle,
        },
      ],
      legend: [
        {
          data: ervLegendData,
          top: 4,
          left: '27%',
          orient: 'horizontal',
          textStyle: { fontSize: 11 },
          itemWidth: 14, itemHeight: 10, itemGap: 10,
        },
        {
          data: groupLegendData,
          top: 4,
          left: '60%',
          orient: 'horizontal',
          textStyle: { fontSize: 11 },
          itemWidth: 14, itemHeight: 10, itemGap: 10,
        },
      ],
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow', xAxisIndex: 0 },
        formatter(params) {
          const barParam = params.find((p) => p.seriesName === gammaName || p.seriesName === betaName);
          const idx = barParam ? barParam.dataIndex : params[0].dataIndex;
          const it = list[idx];
          if (!it) return params[0].name;
          const g = it.gamma_count != null ? it.gamma_count : 0;
          const b = it.beta_count != null ? it.beta_count : 0;
          const grp = it.group ? `<br/>Group: ${escHtml(it.group)}` : '';
          const asmLine = it.assembly
            ? `<br/><span style="color:#94a3b8;font-size:11px;">${escHtml(it.assembly)}</span>`
            : '';
          const hint = '<br/><span style="color:#94a3b8;font-size:10px;">Click bar to pin &amp; open link</span>';
          return `<strong>${escHtml(it.full_name || it.name)}</strong> (${escHtml(it.name)})${grp}<br/>`
            + `${escHtml(gammaName)}: ${g}<br/>${escHtml(betaName)}: ${b}<br/>Total: ${it.count}${asmLine}${hint}`;
        },
      },
      xAxis: [
        {
          type: 'category',
          data: categories,
          boundaryGap: true,
          axisTick: { alignWithLabel: true },
          axisLabel: {
            interval: 0,
            rotate: 0,
            margin: 8,
            align: 'center',
            formatter(abbr) {
              const it = list.find((x) => x.name === abbr);
              if (!it) return abbr;
              const key = groupLabelRichKey(it.group);
              return `{${key}|${abbr}}`;
            },
            rich: {
              east:  { fontSize: 10, fontWeight: 'bold', color: PALETTE.east  },
              west:  { fontSize: 10, fontWeight: 'bold', color: PALETTE.west  },
              wild:  { fontSize: 10, fontWeight: 'bold', color: PALETTE.wild  },
              other: { fontSize: 10, fontWeight: 'bold', color: '#64748b'     },
            },
          },
        },
      ],
      yAxis: { type: 'value', min: 0 },
      series: [
        {
          name: gammaName,
          type: 'bar',
          xAxisIndex: 0,
          stack: 'breed',
          barWidth: '78%',
          ...barAnimExtra(),
          data: list.map((x) => {
            const beta = x.beta_count != null ? x.beta_count : 0;
            return {
              value: x.gamma_count != null ? x.gamma_count : x.count,
              itemStyle: {
                color: PALETTE.gamma,
                borderRadius: beta === 0 ? [3, 3, 0, 0] : [0, 0, 0, 0],
              },
            };
          }),
          label: {
            show: true,
            position: 'top',
            fontSize: 9,
            color: '#64748b',
            formatter: gammaLabel,
          },
        },
        {
          name: betaName,
          type: 'bar',
          xAxisIndex: 0,
          stack: 'breed',
          barWidth: '78%',
          ...barAnimExtra(),
          data: list.map((x) => (x.beta_count != null ? x.beta_count : 0)),
          itemStyle: { color: PALETTE.beta, borderRadius: [3, 3, 0, 0] },
          label: {
            show: true,
            position: 'top',
            fontSize: 9,
            color: '#64748b',
            formatter: betaLabel,
          },
        },
        legendOnlyLineSeries('Eastern', PALETTE.east, n),
        legendOnlyLineSeries('Western', PALETTE.west, n),
        legendOnlyLineSeries('Wild', PALETTE.wild, n),
        zeroLabelSeries,
      ],
    }, true);

    // Avoid stacking handlers when overview/browser re-renders the chart.
    if (chart.__abbrLegendHandler) {
      chart.off('legendselectchanged', chart.__abbrLegendHandler);
    }
    chart.__abbrLegendHandler = (params) => {
      const sel = params.selected || {};
      if (gammaName in sel) labelSel.gamma = sel[gammaName] !== false;
      if (betaName in sel) labelSel.beta = sel[betaName] !== false;
      // Force labels to re-render with the updated visibility state.
      chart.setOption({
        ...barAnimExtra(),
        series: [
          { label: { formatter: (p) => gammaLabel(p) } },
          { label: { formatter: (p) => betaLabel(p) } },
        ],
      });
    };
    chart.on('legendselectchanged', chart.__abbrLegendHandler);

    if (chart.__abbrClickHandler) {
      chart.off('click', chart.__abbrClickHandler);
    }
    chart.__abbrClickHandler = (params) => {
      if (params.componentType !== 'series') return;
      if (params.seriesName !== gammaName && params.seriesName !== betaName) return;
      chart.__abbrPinFromSeries = true;
      const idx = params.dataIndex;
      if (chart.__abbrPinnedIndex === idx) {
        hidePinCard(chart);
      } else {
        showPinCard(chart, idx, gammaName, betaName);
      }
      setTimeout(() => { chart.__abbrPinFromSeries = false; }, 0);
    };
    chart.on('click', chart.__abbrClickHandler);

    if (!chart.__abbrZrClickBound) {
      chart.__abbrZrClickBound = true;
      chart.getZr().on('click', () => {
        if (chart.__abbrPinFromSeries) return;
        if (chart.__abbrPinnedIndex != null) hidePinCard(chart);
      });
    }

    // Re-pin after data refresh if the same abbr still exists.
    if (chart.__abbrPinnedName) {
      const keepIdx = list.findIndex((x) => x.name === chart.__abbrPinnedName);
      if (keepIdx >= 0) {
        showPinCard(chart, keepIdx, gammaName, betaName);
      } else {
        hidePinCard(chart);
      }
    } else {
      hidePinCard(chart);
    }

    // NOTE: do NOT call chart.resize() here — a synchronous resize right after
    // setOption cancels the bar grow-in animation on first load. The window
    // 'resize' listener (above) keeps the chart responsive afterwards.
    return chart;
  }

  global.AbbrBreedChart = {
    render: renderAbbrBreedChart,
    reflow: reflowChart,
    readPalette,
  };
}(window));
