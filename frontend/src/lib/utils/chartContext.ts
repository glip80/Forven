// Convert a backend chart context (live preview or persisted result) into the
// props ChartWorkspace expects. Mirrors the inline mapping in the lab strategy
// detail page so the live Strategy Creator preview renders identically.
import type { PreviewChartContext } from '$lib/api';
import type { BacktestChartIndicator, BacktestChartMarker } from '$lib/api/backtesting';
import type { IndicatorConfig, SignalMarker } from '$lib/stores/chartStore';

export function toSignalMarkers(
	markers: BacktestChartMarker[] | undefined,
	type: SignalMarker['type']
): SignalMarker[] {
	return (markers ?? [])
		.filter((m) => typeof m.timestamp === 'string' && Number.isFinite(m.price))
		.map((m) => ({
			timestamp: m.timestamp,
			price: m.price,
			type,
			// Preserve trade side so the chart draws shorts/covers correctly.
			direction: m.direction === 'short' ? 'short' : m.direction === 'long' ? 'long' : undefined,
		}));
}

export function toWorkspaceIndicators(
	indicators: BacktestChartIndicator[] | undefined,
	panel: IndicatorConfig['panel']
): IndicatorConfig[] {
	return (indicators ?? []).map((indicator, index) => ({
		id: `${panel}-${indicator.name}-${index}`,
		name: indicator.name,
		params: {},
		color: indicator.color || '#22d3ee',
		panel,
		visible: true,
		data: indicator.data ?? [],
		isStrategyIndicator: true,
	}));
}

export interface ChartWorkspaceProps {
	data: PreviewChartContext['bars'];
	entryMarkers: SignalMarker[];
	exitMarkers: SignalMarker[];
	mainIndicators: IndicatorConfig[];
	subIndicators: IndicatorConfig[];
	strategyName: string | null;
	strategyMeta: string | null;
	warnings: string[];
}

export function chartContextToWorkspaceProps(ctx: PreviewChartContext | null): ChartWorkspaceProps {
	if (!ctx) {
		return {
			data: [], entryMarkers: [], exitMarkers: [],
			mainIndicators: [], subIndicators: [],
			strategyName: null, strategyMeta: null, warnings: [],
		};
	}
	return {
		data: ctx.bars ?? [],
		entryMarkers: toSignalMarkers(ctx.entry_markers, 'entry'),
		exitMarkers: toSignalMarkers(ctx.exit_markers, 'exit'),
		mainIndicators: toWorkspaceIndicators(ctx.main_indicators, 'main'),
		subIndicators: toWorkspaceIndicators(ctx.sub_indicators, 'sub1'),
		strategyName: ctx.strategy_name ?? null,
		strategyMeta: ctx.strategy_meta ?? null,
		warnings: ctx.warnings ?? [],
	};
}
