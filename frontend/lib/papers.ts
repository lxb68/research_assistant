export type ParagraphSummary = {
  index?: number;
  summary?: string;
  charCount?: number;
};

export type SplitChunkHeading = {
  heading?: string;
  level?: number;
  position?: number;
};

export type SplitChunk = {
  summary?: string;
  content?: string;
  charCount?: number;
  partIndex?: number;
  totalParts?: number;
  paragraphSummaries?: ParagraphSummary[];
  headings?: SplitChunkHeading[];
  semanticCategory?: string;
};

export type SavedPaper = {
  id?: string;
  source?: string;
  title?: string;
  authors?: string[];
  abstract?: string;
  year?: string;
  keyword?: string;
  venue?: string;
  doi?: string;
  url?: string;
  pdfUrl?: string;
  pdfPath?: string;
  savedAt?: string;
  ccfLevel?: string;
  impactFactor?: number | null;
  metricFiltersIgnored?: boolean;
  customTags?: string[];
  pdfParseWarning?: string;
  markdownPath?: string;
  markdownOutputDir?: string;
  splitChunkCount?: number;
  splitSectionCount?: number;
  splitMinimumLength?: number;
  splitMaximumLength?: number;
  splitChunks?: SplitChunk[];
};
