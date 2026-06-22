<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import {
		getStrategies,
		getPrebuiltStrategies,
		submitBacktest,
		previewSignals,
		getResult,
		getSymbols,
		type Strategy,
		type SignalPreview,
		type BacktestResult,
	} from '$lib/api';
	import { resolveDateRangePreset, estimateBarCount } from '$lib/utils/dateRange';
	import { addToast } from '$lib/stores/processTracker';
	import SymbolInput from '$lib/components/ui/SymbolInput.svelte';
	import TimeframeSelect from '$lib/components/ui/TimeframeSelect.svelte';
	import DateRangeFieldset from '$lib/components/ui/DateRangeFieldset.svelte';
	import ParameterEditor from '$lib/components/ui/ParameterEditor.svelte';
	import BacktestResultSummary from '$lib/components/backtest/BacktestResultSummary.svelte';

	const BAR_CAP = 100_000;

	let prebuiltStrategies: Strategy[] = [];
	let appStrategies: Strategy[] = [];
	let strategies: Strategy[] = [];
	let includeAppGenerated = false;
	let selectedStrategy: Strategy | null = null;
	let selectedKey = '';
	let paramsDraft: Record<string, unknown> = {};
	let loadingStrategies = true;
	let loadError = '';
	let symbolSuggestions: string[] = [];

	// Form state
	const defaultRange = resolveDateRangePreset('1y');
	let symbol = 'BTC/USDT';
	let timeframe = '1h';
	let startDate = defaultRange.startDate;
	let endDate = defaultRange.endDate;

	// Advanced execution config
	let showAdvanced = false;
	let initialCapital = 10000;
	let feeBps = 10;
	let slippageBps = 5;
	let leverage = 1;
	let tradeMode: 'long_only' | 'short_only' | 'both' = 'long_only';
	let sizingMode: 'full' | 'fraction' | 'fixed' | 'atr' | 'kelly' = 'full';
	let riskPerTrade = 0.02;
	let fixedSize = 1000;
	let atrStopMultiplier = 2;
	let kellyMultiplier = 0.5;
	let kellyLookback = 100;
	let stopLossPct: number | null = null;
	let takeProfitPct: number | null = null;
	let trailingStopPct: number | null = null;
	let timeStopBars: number | null = null;

	// Preview state
	let previewLoading = false;
	let preview: SignalPreview | null = null;
	let previewError = '';

	// Submission + result state
	type SubmitStatus = 'idle' | 'submitting' | 'failed';
	let submitStatus: SubmitStatus = 'idle';
	let submitError = '';
	let submitWarning = '';
	let resultLoading = false;
	let inlineResult: BacktestResult | null = null;
	let lastResultId = '';
	let lastStrategyId = '';

	$: busy = submitStatus === 'submitting';
	$: estimatedBars = estimateBarCount(startDate, endDate, timeframe);
	$: numberOrNull = (v: string) => (v.trim() === '' ? null : Number(v));

	function rebuildStrategies() {
		const seen = new Set<string>();
		const merged: Strategy[] = [];
		for (const s of prebuiltStrategies) {
			const key = s.api_name || s.name;
			if (!seen.has(key)) {
				seen.add(key);
				merged.push(s);
			}
		}
		if (includeAppGenerated) {
			for (const s of appStrategies) {
				const key = s.api_name || s.name;
				if (!seen.has(key)) {
					seen.add(key);
					merged.push(s);
				}
			}
		}
		strategies = merged.sort((a, b) => a.name.localeCompare(b.name));
	}

	function toggleAppGenerated() {
		includeAppGenerated = !includeAppGenerated;
		rebuildStrategies();
	}

	async function loadStrategies() {
		loadingStrategies = true;
		loadError = '';
		try {
			const [prebuiltRes, appRes] = await Promise.all([getPrebuiltStrategies(), getStrategies()]);
			prebuiltStrategies = prebuiltRes.strategies;
			appStrategies = appRes.strategies;
			rebuildStrategies();
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load strategies';
		} finally {
			loadingStrategies = false;
		}
	}

	async function loadSymbols() {
		try {
			symbolSuggestions = await getSymbols();
		} catch {
			symbolSuggestions = [];
		}
	}

	onMount(() => {
		loadStrategies();
		loadSymbols();
	});

	function applyStrategy(key: string) {
		selectedKey = key;
		selectedStrategy = strategies.find((s) => (s.api_name || s.name) === key) ?? null;
		preview = null;
		previewError = '';
		if (selectedStrategy?.parameters) {
			paramsDraft = Object.fromEntries(
				Object.entries(selectedStrategy.parameters).map(([k, spec]) => [k, spec.default]),
			);
		} else {
			paramsDraft = {};
		}
		const sSym = (selectedStrategy as Record<string, unknown> | null)?.symbol;
		const sTf = (selectedStrategy as Record<string, unknown> | null)?.timeframe;
		if (typeof sSym === 'string' && sSym.trim()) symbol = sSym.trim();
		if (typeof sTf === 'string' && sTf.trim()) timeframe = sTf.trim();
	}

	function onStrategySelect(event: Event) {
		applyStrategy((event.target as HTMLSelectElement).value);
	}

	function onParamsChange(event: CustomEvent<Record<string, unknown>>) {
		paramsDraft = event.detail;
		preview = null;
	}

	function validate(): string | null {
		if (!selectedStrategy) return 'Select a strategy to backtest.';
		if (!symbol.trim()) return 'Symbol is required.';
		if (startDate && endDate && startDate >= endDate) return 'Start date must be before end date.';
		if (!Number.isFinite(initialCapital) || initialCapital <= 0) return 'Initial capital must be greater than 0.';
		if (!Number.isFinite(feeBps) || feeBps < 0) return 'Fee (bps) cannot be negative.';
		if (!Number.isFinite(slippageBps) || slippageBps < 0) return 'Slippage (bps) cannot be negative.';
		if (!Number.isFinite(leverage) || leverage < 1) return 'Leverage must be at least 1.';
		if (leverage > 125) return 'Leverage above 125× is not supported.';
		if (sizingMode === 'fraction') {
			if (!(riskPerTrade > 0 && riskPerTrade <= 1)) return 'Risk per trade must be between 0 and 1.';
			if (stopLossPct == null && trailingStopPct == null)
				return 'Fraction (risk-based) sizing needs a Stop Loss % or Trailing Stop %.';
		}
		if (sizingMode === 'fixed' && !(fixedSize > 0)) return 'Fixed size must be greater than 0.';
		if (sizingMode === 'atr') {
			if (!(atrStopMultiplier > 0)) return 'ATR stop multiplier must be greater than 0.';
			if (!(riskPerTrade > 0 && riskPerTrade <= 1)) return 'Risk per trade must be between 0 and 1.';
		}
		if (sizingMode === 'kelly') {
			if (!(kellyMultiplier > 0 && kellyMultiplier <= 5)) return 'Kelly multiplier must be between 0 and 5.';
			if (!(Number.isInteger(kellyLookback) && kellyLookback >= 1)) return 'Kelly lookback must be a positive whole number.';
		}
		if (stopLossPct != null && !(stopLossPct > 0 && stopLossPct <= 100)) return 'Stop Loss % must be between 0 and 100.';
		if (takeProfitPct != null && !(takeProfitPct > 0)) return 'Take Profit % must be greater than 0.';
		if (trailingStopPct != null && !(trailingStopPct > 0 && trailingStopPct <= 100)) return 'Trailing Stop % must be between 0 and 100.';
		if (timeStopBars != null && !(Number.isInteger(timeStopBars) && timeStopBars >= 1)) return 'Time Stop must be a positive whole number of bars.';
		if (estimatedBars != null && estimatedBars > BAR_CAP)
			return `This window is ~${estimatedBars.toLocaleString()} bars; the engine caps at ${BAR_CAP.toLocaleString()}.`;
		return null;
	}

	function buildRequest() {
		const strategyId = selectedStrategy!.api_name || selectedStrategy!.name;
		return {
			strategy_id: strategyId,
			strategy_name: strategyId,
			strategy_version: selectedStrategy!.version,
			symbol: symbol.trim(),
			timeframe,
			start: startDate,
			end: endDate,
			params: Object.keys(paramsDraft).length > 0 ? paramsDraft : undefined,
			preserve_result: true,
			initial_capital: initialCapital,
			fee_bps: feeBps,
			slippage_bps: slippageBps,
			leverage,
			trade_mode: tradeMode,
			allow_shorting: tradeMode !== 'long_only',
			sizing_mode: sizingMode,
			risk_per_trade: sizingMode === 'fraction' || sizingMode === 'atr' ? riskPerTrade : undefined,
			fixed_size: sizingMode === 'fixed' ? fixedSize : undefined,
			atr_stop_multiplier: sizingMode === 'atr' ? atrStopMultiplier : undefined,
			kelly_multiplier: sizingMode === 'kelly' ? kellyMultiplier : undefined,
			kelly_lookback: sizingMode === 'kelly' ? kellyLookback : undefined,
			stop_loss_pct: stopLossPct,
			take_profit_pct: takeProfitPct,
			trailing_stop_pct: trailingStopPct,
			time_stop_bars: timeStopBars,
		};
	}

	async function handlePreview() {
		if (!selectedStrategy) {
			previewError = 'Select a strategy first.';
			return;
		}
		previewLoading = true;
		previewError = '';
		preview = null;
		try {
			preview = await previewSignals({
				strategy_name: selectedStrategy.api_name || selectedStrategy.name,
				strategy_version: selectedStrategy.version,
				symbol: symbol.trim(),
				timeframe,
				start: startDate,
				end: endDate,
				trade_mode: tradeMode,
				params: Object.keys(paramsDraft).length > 0 ? paramsDraft : undefined,
			});
		} catch (err) {
			previewError = err instanceof Error ? err.message : 'Signal preview failed';
		} finally {
			previewLoading = false;
		}
	}

	async function handleSubmit() {
		const error = validate();
		if (error) {
			submitError = error;
			return;
		}
		submitStatus = 'submitting';
		submitError = '';
		submitWarning = '';
		inlineResult = null;
		const request = buildRequest();
		const strategyId = request.strategy_id;
		try {
			const job = await submitBacktest(request);
			lastStrategyId = strategyId;
			if (job.warning) submitWarning = job.warning;
			if (job.status === 'succeeded') addToast(`Backtest for ${strategyId} completed`, 'success');
			else addToast(`Backtest for ${strategyId} queued (job ${job.job_id})`, 'info');
			submitStatus = 'idle';
			if (job.result_id) {
				lastResultId = job.result_id;
				resultLoading = true;
				try {
					inlineResult = await getResult(job.result_id);
				} catch {
					inlineResult = null;
				} finally {
					resultLoading = false;
				}
				queueMicrotask(() => document.getElementById('bt-results')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
			}
		} catch (err) {
			submitStatus = 'failed';
			submitError = err instanceof Error ? err.message : 'Backtest submission failed';
		}
	}

	function openFullReport() {
		if (!lastStrategyId) return;
		goto(`/lab/strategy/${encodeURIComponent(lastStrategyId)}?returnTo=/backtest/new`);
	}

	function resetForNextRun() {
		inlineResult = null;
		lastResultId = '';
		submitWarning = '';
		queueMicrotask(() => document.getElementById('bt-config')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
	}
</script>

<svelte:head>
	<title>Manual Backtest | Forven</title>
</svelte:head>

<div class="min-h-screen bg-[#050505] px-4 py-8 md:px-8">
	<div class="mx-auto max-w-4xl">
		<!-- Header -->
		<div class="rounded-2xl border border-[#1a1a1a] bg-gradient-to-b from-[#0d0d0d] to-[#080808] px-6 py-5">
			<div class="flex flex-wrap items-center justify-between gap-4">
				<div>
					<h1 class="text-xl font-semibold text-white">Manual Backtest</h1>
					<p class="mt-1 text-sm text-gray-500">
						Pick a strategy, configure execution, preview signals, and run — results appear inline.
					</p>
				</div>
				<a href="/strategy-creator"
					class="inline-flex items-center gap-2 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-2 text-[12px] font-medium text-cyan-200 transition hover:bg-cyan-500/20">
					✨ Build your own → Strategy Creator
				</a>
			</div>
		</div>

		<form id="bt-config" on:submit|preventDefault={handleSubmit} novalidate>
			<!-- Strategy Selection -->
			<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
				<div class="flex items-center justify-between gap-3">
					<div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">
						Strategy
						<span class="ml-2 rounded bg-[#1a1a1a] px-1.5 py-0.5 text-[10px] tabular-nums text-gray-400">{strategies.length}</span>
					</div>
					<button
						type="button"
						on:click={toggleAppGenerated}
						disabled={busy}
						aria-pressed={includeAppGenerated}
						class="inline-flex items-center gap-2 rounded border px-3 py-1 text-[11px] transition {includeAppGenerated
							? 'border-cyan-500/40 bg-cyan-500/10 text-cyan-300'
							: 'border-[#2b2b2b] bg-[#111] text-gray-500 hover:border-gray-600 hover:text-gray-300'}"
					>
						<span class="inline-block h-2 w-2 rounded-full {includeAppGenerated ? 'bg-cyan-400' : 'bg-gray-600'}"></span>
						Include app-generated strategies
					</button>
				</div>
				<div class="mt-3">
					{#if loadingStrategies}
						<div class="text-sm text-gray-500" role="status" aria-live="polite">Loading strategies…</div>
					{:else if loadError}
						<div class="flex flex-wrap items-center gap-3" role="alert">
							<span class="text-sm text-red-400">{loadError}</span>
							<button type="button" on:click={loadStrategies}
								class="rounded border border-[#2b2b2b] bg-[#111] px-3 py-1 text-[11px] text-gray-300 transition hover:border-gray-600 hover:text-white">Retry</button>
						</div>
					{:else}
						<select
							id="bt-strategy"
							class="w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none transition focus:border-white/60"
							on:change={onStrategySelect}
							disabled={busy}
							value={selectedKey}
						>
							<option value="" disabled>Select a strategy…</option>
							{#each strategies as strategy}
								<option value={strategy.api_name || strategy.name}>
									{strategy.name}{strategy.api_name && strategy.api_name !== strategy.name ? ` (${strategy.api_name})` : ''}
								</option>
							{/each}
						</select>
						{#if selectedStrategy?.description}
							<div class="mt-2 text-xs text-gray-500">{selectedStrategy.description}</div>
						{/if}
					{/if}
				</div>
			</div>

			<!-- Market Scope -->
			<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
				<div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Market Scope</div>
				<div class="mt-3 grid gap-4 md:grid-cols-2">
					<SymbolInput id="bt-symbol" bind:value={symbol} disabled={busy} suggestions={symbolSuggestions} helpText="Backtested on the base asset (e.g. BTC)." />
					<TimeframeSelect id="bt-timeframe" bind:value={timeframe} disabled={busy} />
				</div>
				<div class="mt-4">
					<DateRangeFieldset idPrefix="bt-date" bind:startDate bind:endDate {timeframe} />
				</div>
			</div>

			<!-- Strategy Parameters -->
			{#if selectedStrategy}
				<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
					<div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Strategy Parameters</div>
					<div class="mt-3">
						<ParameterEditor params={paramsDraft} saving={busy} on:paramsChange={onParamsChange} />
					</div>
				</div>
			{/if}

			<!-- Advanced Execution Config -->
			<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
				<button type="button" class="flex w-full items-center justify-between text-left" on:click={() => (showAdvanced = !showAdvanced)} aria-expanded={showAdvanced}>
					<div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Execution Settings</div>
					<span class="text-sm text-gray-500">{showAdvanced ? '−' : '+'}</span>
				</button>
				{#if showAdvanced}
					<div class="mt-4 text-[10px] uppercase tracking-[0.2em] text-gray-600">Capital &amp; Costs</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Initial Capital</div>
							<input type="number" bind:value={initialCapital} step="1000" min="100" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Fee (bps)</div>
							<input type="number" bind:value={feeBps} step="1" min="0" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Slippage (bps)</div>
							<input type="number" bind:value={slippageBps} step="1" min="0" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Leverage</div>
							<input type="number" bind:value={leverage} step="0.5" min="1" max="125" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Trade Direction</div>
							<select bind:value={tradeMode} disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60">
								<option value="long_only">Long only</option><option value="short_only">Short only</option><option value="both">Both (hedged)</option>
							</select></label>
					</div>
					<div class="mt-5 text-[10px] uppercase tracking-[0.2em] text-gray-600">Position Sizing</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-3">
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Sizing Mode</div>
							<select bind:value={sizingMode} disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60">
								<option value="full">Full equity (default)</option><option value="fraction">Fraction (risk-based)</option><option value="fixed">Fixed notional</option><option value="atr">ATR risk</option><option value="kelly">Kelly</option>
							</select></label>
						{#if sizingMode === 'fraction' || sizingMode === 'atr'}
							<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Risk Per Trade</div>
								<input type="number" bind:value={riskPerTrade} step="0.005" min="0" max="1" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						{/if}
						{#if sizingMode === 'fixed'}
							<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Fixed Size (quote)</div>
								<input type="number" bind:value={fixedSize} step="100" min="0" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						{/if}
						{#if sizingMode === 'atr'}
							<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">ATR Stop Multiplier</div>
								<input type="number" bind:value={atrStopMultiplier} step="0.1" min="0" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						{/if}
						{#if sizingMode === 'kelly'}
							<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Kelly Multiplier</div>
								<input type="number" bind:value={kellyMultiplier} step="0.05" min="0" max="5" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
							<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Kelly Lookback (trades)</div>
								<input type="number" bind:value={kellyLookback} step="10" min="1" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						{/if}
					</div>
					<div class="mt-5 text-[10px] uppercase tracking-[0.2em] text-gray-600">Exits &amp; Stops</div>
					<div class="mt-2 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Stop Loss %</div>
							<input type="number" value={stopLossPct ?? ''} on:input={(e) => (stopLossPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Take Profit %</div>
							<input type="number" value={takeProfitPct ?? ''} on:input={(e) => (takeProfitPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" placeholder="None" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Trailing Stop %</div>
							<input type="number" value={trailingStopPct ?? ''} on:input={(e) => (trailingStopPct = numberOrNull(e.currentTarget.value))} step="0.5" min="0" max="100" placeholder="None" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
						<label class="block"><div class="text-[10px] uppercase tracking-[0.2em] text-gray-500">Time Stop (bars)</div>
							<input type="number" value={timeStopBars ?? ''} on:input={(e) => (timeStopBars = numberOrNull(e.currentTarget.value))} step="1" min="1" placeholder="None" disabled={busy} class="mt-1.5 w-full rounded border border-[#2b2b2b] bg-[#050505] px-3 py-2 text-sm text-white outline-none focus:border-white/60" /></label>
					</div>
				{/if}
			</div>

			<!-- Preview -->
			<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
				<div class="flex items-center justify-between">
					<div class="text-[10px] uppercase tracking-[0.24em] text-gray-500">Signal Preview</div>
					<button type="button" on:click={handlePreview} disabled={busy || previewLoading || !selectedStrategy}
						class="rounded border border-[#2b2b2b] bg-[#111] px-3 py-1 text-[11px] text-gray-300 transition hover:border-gray-600 hover:text-white disabled:opacity-40">
						{previewLoading ? 'Previewing…' : 'Preview signals'}
					</button>
				</div>
				{#if previewError}
					<div class="mt-3 rounded border border-red-900/40 bg-red-950/20 px-3 py-2 text-[11px] text-red-300" role="alert">{previewError}</div>
				{:else if preview}
					<div class="mt-3 grid grid-cols-2 gap-2 text-[11px] sm:grid-cols-4">
						<div><span class="text-gray-500">Bars:</span> <span class="font-mono text-gray-300">{preview.total_bars.toLocaleString()}</span></div>
						<div><span class="text-gray-500">Entries:</span> <span class="font-mono text-cyan-300">{preview.entry_count}</span></div>
						<div><span class="text-gray-500">Exits:</span> <span class="font-mono text-gray-300">{preview.exit_count}</span></div>
						<div><span class="text-gray-500">Density:</span>
							<span class="font-mono {preview.signal_density === 'dense' ? 'text-emerald-300' : preview.signal_density === 'moderate' ? 'text-amber-300' : 'text-gray-400'}">{preview.signal_density}</span></div>
					</div>
					{#if preview.warnings.length}
						<div class="mt-2 space-y-1">
							{#each preview.warnings as w}<div class="rounded border border-amber-900/40 bg-amber-950/20 px-3 py-1.5 text-[11px] text-amber-300">{w}</div>{/each}
						</div>
					{/if}
				{:else}
					<p class="mt-2 text-[11px] text-gray-600">Check signal density and data coverage for your config before committing to a full run.</p>
				{/if}
			</div>

			<!-- Submit -->
			<div class="mt-6 rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-5">
				{#if submitError}<div class="mb-4 rounded-xl border border-red-900/40 bg-red-950/20 px-4 py-3 text-sm text-red-300" role="alert">{submitError}</div>{/if}
				<div class="flex flex-wrap items-center gap-3">
					<button type="submit" disabled={busy || resultLoading || !selectedStrategy} aria-busy={busy}
						class="inline-flex items-center gap-2 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-6 py-2.5 text-sm font-medium text-cyan-200 transition hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-40">
						{#if busy || resultLoading}Running backtest…{:else}Run Backtest{/if}
					</button>
				</div>
			</div>
		</form>

		<!-- Inline results -->
		{#if resultLoading || inlineResult || submitWarning}
			<div id="bt-results" class="mt-8 scroll-mt-6">
				{#if submitWarning}
					<div class="mb-4 rounded-xl border border-amber-900/40 bg-amber-950/20 px-4 py-3 text-sm text-amber-300" role="alert">⚠ {submitWarning}</div>
				{/if}
				<div class="rounded-2xl border border-[#1a1a1a] bg-gradient-to-b from-[#0d0d0d] to-[#080808] px-6 py-5">
					<div class="flex flex-wrap items-center justify-between gap-3">
						<div>
							<h2 class="text-lg font-semibold text-white">Result</h2>
							<p class="mt-0.5 text-xs text-gray-500">Out-of-sample performance for the run you just submitted.</p>
						</div>
						<div class="flex items-center gap-2">
							<button type="button" on:click={resetForNextRun}
								class="rounded border border-[#2b2b2b] bg-[#111] px-3 py-1.5 text-[11px] text-gray-300 transition hover:border-gray-600 hover:text-white">Adjust &amp; re-run</button>
							<button type="button" on:click={openFullReport} disabled={!lastStrategyId}
								class="rounded border border-cyan-500/40 bg-cyan-500/10 px-3 py-1.5 text-[11px] text-cyan-200 transition hover:bg-cyan-500/20 disabled:opacity-40">Open full report →</button>
						</div>
					</div>
				</div>
				<div class="mt-4">
					{#if resultLoading}
						<div class="rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-8 text-center text-sm text-gray-500" role="status" aria-live="polite">Loading result…</div>
					{:else if inlineResult}
						<BacktestResultSummary result={inlineResult} />
					{:else if lastResultId}
						<div class="rounded-2xl border border-[#1a1a1a] bg-[#0a0a0a] p-6 text-sm text-gray-400">
							Result saved (<span class="font-mono">{lastResultId}</span>) but the summary could not be loaded here.
							<button type="button" on:click={openFullReport} class="ml-1 text-cyan-300 underline">Open the full report</button>.
						</div>
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>
