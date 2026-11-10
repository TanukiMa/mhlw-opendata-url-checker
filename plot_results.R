# Plot the output of check_urls.py (results/all_results.csv, results/summary.csv)
# per facility type. For journal submission it writes a colour and a monochrome
# (greyscale) edition, each in English and Japanese.
#
#   Rscript plot_results.R                         # results/ -> figs/<color|mono>/<en|ja>/
#   Rscript plot_results.R results figs            # input and output folders
#   Rscript plot_results.R results figs notitle    # no in-figure title/subtitle/caption
#                                                  # (when captions go in the manuscript)
#   Rscript plot_results.R results figs mono       # monochrome only ("color" = colour only;
#                                                  # default: both)
#
# Output: figs/<color|mono>/<en|ja>/<code>_<type>_<chart>.png (600 dpi, ragg) and .svg (svglite)
#         figs/<color|mono>/<en|ja>/00_all_*.png/.svg (comparison across facility types)
# Fonts:  English edition = Verdana; Japanese edition = Meiryo for text + Verdana for numbers
#         (Meiryo's Latin glyphs are derived from Verdana, so the two match).
#
# The category labels in the input CSVs are Japanese (written by check_urls.py).
# The Japanese edition uses them as they are; the English edition translates them
# with EN_MAP below.
#
# Required packages: ggplot2 svglite ragg systemfonts readr dplyr tidyr scales forcats stringr

suppressPackageStartupMessages({
  library(ggplot2); library(dplyr); library(tidyr); library(readr)
  library(stringr); library(forcats); library(scales)
})

args      <- commandArgs(trailingOnly = TRUE)
SHOW_TITLE <- !("notitle" %in% args)
MODES <- intersect(c("color", "mono"), args); if (!length(MODES)) MODES <- c("color", "mono")
args <- setdiff(args, c("notitle", "color", "mono"))
in_dir    <- if (length(args) >= 1) args[1] else "results"
out_root  <- if (length(args) >= 2) args[2] else "figs"

DPI <- 600
W   <- 16    # figure width in cm

# Input file -> facility type (Japanese label as written by check_urls.py), file-name
# prefix, and English label
KINDS <- tibble::tribble(
  ~file,                                       ~kind,        ~code, ~slug,       ~kind_en,
  "01-1_hospital_facility_info_20260601.csv",  "病院",       "01",  "hospital",  "Hospitals",
  "02-1_clinic_facility_info_20260601.csv",    "診療所",     "02",  "clinic",    "Clinics",
  "03-1_dental_facility_info_20260601.csv",    "歯科診療所", "03",  "dental",    "Dental clinics",
  "04_maternity_home_20260601.csv",            "助産所",     "04",  "maternity", "Maternity homes",
  "05_pharmacy_20260601.csv",                  "薬局",       "05",  "pharmacy",  "Pharmacies",
)

# ------------------------------------------------------------------ Colours
WHITE <- "#ffffff"; INK <- "#000000"; INK2 <- "#333333"; MUTED <- "#666666"; GRID <- "#d9d9d9"
grays <- function(n) {               # monochrome: dark -> light, evenly spaced for n categories
  if (n == 1) return("#4d4d4d")
  gray.colors(n, start = 0.12, end = 0.88, gamma = 1)
}
# Colour: categorical reference palette, ordered so that adjacent pairs stay
# distinguishable under colour-vision deficiency (checked with a palette validator).
# Yellow, magenta and aqua have < 3:1 contrast against white, so values and legends
# are always labelled directly instead of relying on colour alone.
CAT <- c("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
NONE_GRAY <- "#bfbfbf"               # neutral colour for "absent" categories such as "No URL"
text_on <- function(fill) {          # white or black value labels depending on fill lightness
  l <- colSums(col2rgb(fill) * c(0.299, 0.587, 0.114)) / 255
  ifelse(l < 0.55, WHITE, INK)
}

# ------------------------------------------------------------------ Translation
# Japanese category labels produced by check_urls.py -> English labels
EN_MAP <- c(
  # URL listed or not
  "記載あり" = "URL listed", "記載なし" = "No URL",
  # Overall result (stage)
  "OK" = "OK", "OK（要修正あり）" = "OK after correction",
  "到達可（自動アクセス拒否）" = "Server responded (access denied)",
  "HTTPで到達不可" = "HTTP unreachable", "DNSで不正" = "DNS failure",
  "URL構文で不正（DNSは解決可）" = "Invalid URL syntax (DNS resolvable)",
  "URL/ホスト名構文で不正" = "Invalid URL/hostname syntax",
  # Scheme as written
  "正しく記載" = "Present", "欠落" = "Missing", "誤記（修正）" = "Misspelled (corrected)",
  # Correct scheme for URLs without one
  "https が正（http は https へ転送）" = "HTTPS (HTTP redirects to HTTPS)",
  "https が正（http でも到達可・転送なし）" = "HTTPS (HTTP also works, no redirect)",
  "https が正（http では到達不可）" = "HTTPS (HTTP unreachable)",
  "http が正（https は証明書不正）" = "HTTP (HTTPS certificate invalid)",
  "http が正（https では到達不可）" = "HTTP (HTTPS unreachable)",
  "https のみ到達可（証明書不正）" = "HTTPS only (certificate invalid)",
  "どちらも到達不可" = "Neither reachable",
  "未判定（DNS解決不可）" = "Undetermined (DNS failure)",
  "未判定（URL修正不能）" = "Undetermined (unrecoverable URL)",
  # DNS
  "ドメインが存在しない" = "Domain does not exist",
  "ホスト名が存在しない" = "Hostname does not exist",
  "ドメインは有るがホスト名が無い" = "Domain exists, hostname does not",
  "名前は存在するがアドレスなし" = "Name exists, no address record",
  "DNS応答異常(SERVFAIL)" = "DNS error (SERVFAIL)", "DNS応答異常(TIMEOUT)" = "DNS error (timeout)",
  "未照会（ホスト名構文不正）" = "Not queried (invalid hostname)",
  "未照会（ホスト名なし）" = "Not queried (no hostname)",
  # URL notation errors
  "スキーム欠落" = "Missing scheme", "スキーム誤記" = "Misspelled scheme",
  "URLを抽出できない（修正不能）" = "Unrecoverable (no URL extracted)",
  "ホスト名末尾にドット" = "Trailing dot in hostname", "ホスト名先頭にドット" = "Leading dot in hostname",
  "パスに // を含む" = "Double slash in path", "スキーム二重記述" = "Duplicated scheme",
  "ホスト名: ドット連続（..）" = "Consecutive dots in hostname",
  "ホスト名: ドットの代わりにカンマ" = "Comma instead of dot",
  "www の後の区切り誤記" = "Wrong separator after www",
  "URL中に空白/制御文字" = "Whitespace in URL", "前後に空白" = "Leading/trailing whitespace",
  "抽出結果と原文が不一致" = "Extra text around URL",
  "部分抽出のため不採用" = "Partial extraction (rejected)",
  "複数URL" = "Multiple URLs", "非HTTPスキーム" = "Non-HTTP scheme", "ホスト名なし" = "No hostname",
  # Hostname and TLD
  "実在しないTLD" = "Nonexistent TLD", "ドットなし（TLDなし）" = "No dot (no TLD)",
  "使用不可文字を含む" = "Invalid characters", "アンダースコアを含む" = "Underscore",
  "ラベル先頭/末尾がハイフン" = "Label starts/ends with hyphen",
  "URLではない記述（日本語等の文字列）" = "Not a URL (Japanese text etc.)",
  "IPアドレス直書き" = "IP address literal",
  "公開サフィックスのみ（登録ドメインなし）" = "Public suffix only",
  "全角/半角カナ・全角記号が混入" = "Full-width/kana characters",
  "空ラベル（.. や先頭/末尾のドット）" = "Empty label", "空ラベル（.. 等）" = "Empty label",
  "IDN変換不可（使用不可文字）" = "IDN conversion failed",
  "国際化ドメイン名" = "Internationalized domain name",
  "ホスト名が253文字超" = "Hostname > 253 chars", "ラベルが63文字超" = "Label > 63 chars"
)

# ------------------------------------------------------------------ Input
res <- read_csv(file.path(in_dir, "all_results.csv"), col_types = cols(.default = col_character()),
                na = character(), progress = FALSE) |>
  select(-any_of(c("kind", "code"))) |> inner_join(KINDS, by = "file")
smy <- read_csv(file.path(in_dir, "summary.csv"), col_types = cols(.default = col_character()),
                na = character(), progress = FALSE)
# summary.csv uses English column names (total_rows, url_not_listed). Older versions of
# check_urls.py wrote Japanese names: 総行数 = total rows, URL記載なし (or URL空白) = no URL.
smy_cols <- names(smy)
smy <- smy |> filter(file %in% KINDS$file) |>
  select(-any_of(c("kind", "code", "種別", "facility_type"))) |> inner_join(KINDS, by = "file") |>
  mutate(total = as.integer(if ("total_rows" %in% smy_cols) total_rows else 総行数),
         none  = as.integer(if ("url_not_listed" %in% smy_cols) url_not_listed
                            else if ("URL記載なし" %in% smy_cols) URL記載なし else URL空白),
         has   = total - none)
kinds_present <- KINDS |> filter(file %in% res$file)

# ------------------------------------------------------------------ Helpers
# Overall result categories in display order
STAGE_LEVELS <- c("OK", "OK（要修正あり）", "到達可（自動アクセス拒否）", "HTTPで到達不可",
                  "DNSで不正", "URL構文で不正（DNSは解決可）", "URL/ホスト名構文で不正")
clean_stage <- function(s) str_remove(s, "^[0-9-]+'?\\s*")   # drop the stage-number prefix

count_split <- function(x) {   # "a / b(detail) / c" -> count per category (once per row)
  tibble(id = seq_along(x), v = x) |> filter(v != "") |>
    mutate(v = str_split(v, " / ")) |> unnest(v) |>
    mutate(cat = str_remove(v, "\\(.*$")) |> distinct(id, cat) |> count(cat, name = "n")
}

fmt_n   <- function(x) comma(x, accuracy = 1)
fmt_pct <- function(x) ifelse(x > 0 & x < 0.001, "<0.1%", percent(x, accuracy = 0.1))
nlab    <- function(n, p) paste0(fmt_n(n), " (", fmt_pct(p), ")")

# ------------------------------------------------------------------ Draw one edition
render <- function(LANG, MODE) {
  BAR <- if (MODE == "mono") "#4d4d4d" else CAT[1]
  pal <- function(n, absent_last = FALSE) {    # fill colours for n categories
    if (MODE == "mono") return(grays(n))
    if (absent_last) return(c(CAT[seq_len(n - 1)], NONE_GRAY))
    CAT[seq_len(n)]
  }
  out_dir <- file.path(out_root, MODE, LANG); dir.create(out_dir, showWarnings = FALSE, recursive = TRUE)
  JP  <- if (LANG == "ja") "Meiryo" else "Verdana"   # text elements
  NUM <- "Verdana"                                    # numeric elements
  L   <- function(ja, en) if (LANG == "ja") ja else en
  untranslated <- character()
  tr <- function(x) {                          # translate category labels (English edition only)
    if (LANG == "ja") return(x)
    hit <- unname(EN_MAP[x])
    # HTTP status / exception names; "自動アクセス拒否" = access denied
    http <- str_replace(x, "^自動アクセス拒否 ", "Access denied ")
    hit <- ifelse(is.na(hit) & str_detect(x, "^(自動アクセス拒否 )?(HTTP \\d+|[A-Za-z]+)$"), http, hit)
    # "DNS応答異常(X)" = DNS error (X)
    hit <- ifelse(is.na(hit) & str_detect(x, "^DNS応答異常"), str_replace(x, "DNS応答異常", "DNS error "), hit)
    miss <- unique(x[is.na(hit)]); untranslated <<- union(untranslated, miss)
    ifelse(is.na(hit), x, hit)
  }
  kname <- function(k) if (LANG == "ja") k$kind else k$kind_en
  CAPTION <- L("出典: 医療情報ネット オープンデータ（2026-06-01）",
               "Source: Japan Medical Information Net open data (2026-06-01)")

  theme_base <- function() {
    theme_minimal(base_family = JP, base_size = 10) +
      theme(
        plot.background  = element_rect(fill = WHITE, colour = NA),
        panel.background = element_rect(fill = WHITE, colour = NA),
        panel.grid.major.y = element_blank(), panel.grid.minor = element_blank(),
        panel.grid.major.x = element_line(colour = GRID, linewidth = 0.3),
        axis.line.y = element_line(colour = INK2, linewidth = 0.4),
        axis.text   = element_text(colour = INK, size = 8.5),
        axis.text.x = element_text(family = NUM, colour = INK2, size = 7.5),
        axis.title  = element_text(colour = INK2, size = 8.5),
        plot.title  = if (SHOW_TITLE) element_text(colour = INK, size = 12, face = "bold",
                                                   margin = margin(b = 4)) else element_blank(),
        plot.subtitle = if (SHOW_TITLE) element_text(colour = INK2, size = 8.5,
                                                     margin = margin(b = 10)) else element_blank(),
        plot.caption  = if (SHOW_TITLE) element_text(colour = MUTED, size = 7, hjust = 0,
                                                     margin = margin(t = 8)) else element_blank(),
        plot.title.position = "plot", plot.caption.position = "plot",
        legend.text = element_text(colour = INK, size = 8.5), legend.title = element_blank(),
        plot.margin = margin(12, 16, 10, 12)
      )
  }

  save_both <- function(p, name, h = 9) {      # h = height in cm
    if (is.null(p)) return(invisible())
    base <- file.path(out_dir, name)
    ggsave(paste0(base, ".png"), p, device = ragg::agg_png, width = W, height = h,
           units = "cm", dpi = DPI, bg = WHITE)
    ggsave(paste0(base, ".svg"), p, device = svglite::svglite, width = W, height = h,
           units = "cm", bg = WHITE)
    message("  [", MODE, "/", LANG, "] ", name)
  }

  # Horizontal bars (count per category). Ordered by `levels` if given, else by count
  bar_chart <- function(df, title, subtitle, levels = NULL, denom = NULL, xlab = L("件数", "Count")) {
    if (nrow(df) == 0) return(NULL)
    denom <- denom %||% sum(df$n)
    df <- df |> mutate(p = n / denom, lab = nlab(n, p))
    if (!is.null(levels)) {
      lv <- intersect(levels, df$cat); df$cat <- factor(tr(df$cat), levels = rev(tr(lv)))
    } else {
      df$cat <- fct_reorder(tr(df$cat), df$n)
    }
    ggplot(df, aes(x = n, y = cat)) +
      geom_col(fill = BAR, width = 0.62) +
      geom_text(aes(label = lab), hjust = -0.06, family = NUM, size = 2.6, colour = INK) +
      scale_x_continuous(labels = fmt_n, expand = expansion(mult = c(0, 0.32))) +
      labs(title = title, subtitle = subtitle, x = xlab, y = NULL, caption = CAPTION) +
      theme_base()
  }

  # Donut (composition of 2-3 categories). The legend carries counts and percentages,
  # so the reading never depends on colour/grey level alone
  donut_chart <- function(df, title, subtitle, center, absent_last = FALSE) {
    cols <- pal(nrow(df), absent_last)
    df <- df |> mutate(p = n / sum(n), lab = paste0(tr(cat), "  ", nlab(n, p)),
                       lab = factor(lab, levels = lab), txt = if_else(p >= 0.10, fmt_pct(p), ""))
    ggplot(df, aes(x = 2, y = n, fill = lab)) +
      geom_col(width = 1, colour = WHITE, linewidth = 0.8) +
      geom_text(aes(label = txt, colour = lab), position = position_stack(vjust = 0.5),
                family = NUM, size = 2.8, show.legend = FALSE) +
      coord_polar(theta = "y", direction = -1) +
      xlim(0.6, 2.5) +
      annotate("text", x = 0.6, y = 0, label = center, family = JP, size = 3.3, colour = INK,
               lineheight = 1.1) +
      scale_fill_manual(values = setNames(cols, levels(df$lab))) +
      scale_colour_manual(values = setNames(text_on(cols), levels(df$lab)), guide = "none") +
      labs(title = title, subtitle = subtitle, caption = CAPTION) +
      theme_base() +
      theme(axis.text = element_blank(), axis.text.x = element_blank(), axis.title = element_blank(),
            axis.line.y = element_blank(), panel.grid = element_blank(),
            panel.grid.major.x = element_blank(),   # remove the polar grid circles
            legend.position = "right", legend.key.size = unit(0.45, "cm"))
  }

  # 100% stacked horizontal bars (comparison across facility types)
  stack_chart <- function(df, title, subtitle, levels, absent_last = FALSE) {
    cols <- pal(length(levels), absent_last); lv_tr <- tr(levels)
    df <- df |> group_by(kind_lab) |> mutate(p = n / sum(n)) |> ungroup() |>
      mutate(cat = factor(tr(cat), levels = lv_tr))
    ggplot(df, aes(x = p, y = kind_lab, fill = cat)) +
      geom_col(width = 0.62, colour = WHITE, linewidth = 0.5, position = position_stack(reverse = TRUE)) +
      geom_text(aes(label = if_else(p >= 0.06, fmt_pct(p), ""), colour = cat), family = NUM, size = 2.5,
                position = position_stack(vjust = 0.5, reverse = TRUE), show.legend = FALSE) +
      scale_x_continuous(labels = label_percent(), expand = expansion(mult = c(0, 0.01))) +
      scale_fill_manual(values = setNames(cols, lv_tr)) +
      scale_colour_manual(values = setNames(text_on(cols), lv_tr), guide = "none") +
      labs(title = title, subtitle = subtitle, x = NULL, y = NULL, caption = CAPTION) +
      # Legend: one row for <= 3 categories, otherwise two columns, so that long
      # labels do not run off the right edge of the figure
      guides(fill = guide_legend(ncol = if (length(levels) <= 3) length(levels) else 2, byrow = TRUE)) +
      theme_base() + theme(legend.position = "top", legend.justification = "left",
                           legend.location = "plot", legend.key.size = unit(0.4, "cm"),
                           legend.spacing.y = unit(0.1, "cm"))
  }

  # ---------------------------------------------------------------- Per facility type
  for (i in seq_len(nrow(kinds_present))) {
    k <- kinds_present[i, ]; d <- res |> filter(file == k$file); s <- smy |> filter(file == k$file)
    K <- kname(k); pre <- paste0(k$code, "_", k$slug, "_")
    nj <- nrow(d)
    base_sub <- L(paste0("URL記載あり ", fmt_n(nj), " 件が分母"), paste0("Denominator: ", fmt_n(nj), " listed URLs"))

    # 1. URL listed or not (denominator: all facilities)
    if (nrow(s) == 1) {
      save_both(donut_chart(tibble(cat = c("記載あり", "記載なし"), n = c(s$has, s$none)),
                            L(paste0(K, "：URL記載の有無"), paste0(K, ": URL listed or not")),
                            L(paste0("全 ", fmt_n(s$total), " 施設が分母"),
                              paste0("Denominator: all ", fmt_n(s$total), " facilities")),
                            L(paste0("全", fmt_n(s$total), "\n施設"), paste0("n = ", fmt_n(s$total))),
                            absent_last = TRUE),
                paste0(pre, "1_url_presence"), h = 8)
    }

    # 2. Overall result
    st <- d |> mutate(cat = clean_stage(stage)) |> count(cat, name = "n")
    save_both(bar_chart(st, L(paste0(K, "：URLの総合判定"), paste0(K, ": Overall URL validation result")),
                        L(paste0(base_sub, "。どの検証段階で問題が出たか"),
                          paste0(base_sub, "; stage at which validation stopped")),
                        levels = STAGE_LEVELS), paste0(pre, "2_overall"), h = 9)

    # 3. Scheme as written (あり = present, 欠落 = missing, 誤記修正 = misspelled, corrected)
    sc <- d |> mutate(cat = recode(scheme_state, "あり" = "正しく記載", "誤記修正" = "誤記（修正）")) |>
      count(cat, name = "n") |>
      arrange(match(cat, c("正しく記載", "欠落", "誤記（修正）")))
    save_both(donut_chart(sc, L(paste0(K, "：スキーム（http:// 等）の記載"), paste0(K, ": URL scheme (http://, https://)")),
                          base_sub, L(paste0(fmt_n(nj), "\n件"), paste0("n = ", fmt_n(nj)))),
              paste0(pre, "3_scheme"), h = 8)

    # 4. Correct scheme for URLs listed without one
    sv <- d |> filter(scheme_state == "欠落", scheme_verdict != "") |> count(cat = scheme_verdict, name = "n")
    if (nrow(sv) > 0) {
      n_https <- sum(d$scheme_state == "欠落" & d$scheme_correct == "https")
      n_http  <- sum(d$scheme_state == "欠落" & d$scheme_correct == "http")
      r <- fmt_pct(n_https / max(n_https + n_http, 1))
      save_both(bar_chart(sv, L(paste0(K, "：スキーム欠落URLの正しい補完先"), paste0(K, ": Correct scheme for URLs missing one")),
                          L(paste0("スキーム欠落 ", fmt_n(sum(sv$n)), " 件が分母。判定できたうち https が正: ", r),
                            paste0("Denominator: ", fmt_n(sum(sv$n)), " URLs without scheme; HTTPS correct in ", r, " of determined"))),
                paste0(pre, "4_scheme_completion"), h = 9)
    }

    # 5. DNS failures (解決可 = resolved). NXDOMAIN hosts whose registered domain exists
    #    are shown as "domain exists, hostname does not"
    dn <- d |> filter(dns_status != "解決可") |>
      mutate(cat = if_else(dns_status == "ホスト名が存在しない" & str_detect(domain_status, "ホスト名部分が無い"),
                           "ドメインは有るがホスト名が無い", dns_status)) |> count(cat, name = "n")
    n_ok <- sum(d$dns_status == "解決可")
    save_both(bar_chart(dn, L(paste0(K, "：DNSで解決できなかった理由"), paste0(K, ": DNS resolution failures")),
                        L(paste0(base_sub, "。解決可は ", nlab(n_ok, n_ok / nj)),
                          paste0(base_sub, "; resolved: ", nlab(n_ok, n_ok / nj))),
                        denom = nj), paste0(pre, "5_dns"), h = 8)

    # 6. URL notation errors
    up <- count_split(d$url_problems) |> slice_max(n, n = 12, with_ties = FALSE)
    save_both(bar_chart(up, L(paste0(K, "：URL記述の問題（上位12）"), paste0(K, ": URL notation errors (top 12)")),
                        L(paste0(base_sub, "。1件に複数あれば重複計上"), paste0(base_sub, "; multiple errors per URL counted separately")),
                        denom = nj), paste0(pre, "6_url_errors"), h = 10)

    # 7. Hostname and TLD errors
    hp <- count_split(d$host_problems)
    save_both(bar_chart(hp, L(paste0(K, "：ホスト名・TLDの問題"), paste0(K, ": Hostname and TLD errors")),
                        base_sub, denom = nj), paste0(pre, "7_hostname_errors"), h = 8)

    # 8. HTTP non-success responses (denied = 401/403/429, labelled "自動アクセス拒否 HTTP n")
    he <- d |> filter(reachable == "False") |>
      mutate(cat = if_else(denied == "True", paste0("自動アクセス拒否 ", str_extract(http_error, "HTTP \\d+")),
                           str_extract(http_error, "^HTTP \\d+|^[A-Za-z]+"))) |>
      count(cat, name = "n") |> slice_max(n, n = 12, with_ties = FALSE)
    n_dnsok <- sum(d$dns_status == "解決可" & d$check_url != "")
    save_both(bar_chart(he, L(paste0(K, "：HTTPで正常応答しなかった理由（上位12）"), paste0(K, ": HTTP non-success responses (top 12)")),
                        L(paste0("DNS解決できた ", fmt_n(n_dnsok), " 件が分母。自動アクセス拒否はサーバー応答あり"),
                          paste0("Denominator: ", fmt_n(n_dnsok), " URLs resolved by DNS; access denied = server responded")),
                        denom = n_dnsok), paste0(pre, "8_http"), h = 9)
  }

  # ---------------------------------------------------------------- Across facility types
  klab <- function(df) df |> mutate(kind_lab = factor(if (LANG == "ja") kind else kind_en,
                                                      levels = rev(if (LANG == "ja") KINDS$kind else KINDS$kind_en)))
  if (nrow(smy) > 1) {
    pr <- smy |> select(kind, kind_en, has, none) |>
      pivot_longer(c(has, none), names_to = "cat", values_to = "n") |>
      mutate(cat = recode(cat, has = "記載あり", none = "記載なし")) |> klab()
    save_both(stack_chart(pr, L("種別ごとのURL記載の有無", "URL listed or not, by facility type"),
                          L("各種別の全施設が分母（6%未満は数値省略）", "Denominator: all facilities of each type (values < 6% not labelled)"),
                          c("記載あり", "記載なし"), absent_last = TRUE), "00_all_1_url_presence", h = 8)
  }
  if (nrow(kinds_present) > 1) {
    stl <- res |> mutate(cat = clean_stage(stage)) |> count(kind, kind_en, cat, name = "n") |> klab()
    save_both(stack_chart(stl, L("種別ごとのURL総合判定", "Overall URL validation result, by facility type"),
                          L("各種別のURL記載ありが分母（6%未満は数値省略）", "Denominator: listed URLs of each type (values < 6% not labelled)"),
                          intersect(STAGE_LEVELS, unique(stl$cat))), "00_all_2_overall", h = 11)
  }
  if (length(untranslated)) warning("Categories without an English translation (left in Japanese): ",
                                    paste(untranslated, collapse = " | "))
}

for (mode in MODES) for (lang in c("en", "ja")) render(lang, mode)
message("Done: ", normalizePath(out_root))
