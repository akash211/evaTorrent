//! Coordinating `SearchService` managing parallel indexer queries, Hindi/English audio filters,
//! spelling variation fallbacks, health score ranking, and persistent caching.

use std::collections::HashSet;
use std::sync::Arc;

use chrono::{NaiveDate, NaiveDateTime, Utc};
use futures::future::join_all;
use regex::Regex;
use serde_json::{json, Value};

use crate::search::base::{SearchCategory, SearchProvider, SearchResult};
use crate::search::cache::SearchCacheManager;
use crate::search::health::compute_health_score;
use crate::search::providers::{
    EztvSearchProvider, LimeTorrentsSearchProvider, NyaaSearchProvider, PirateBaySearchProvider,
    X1337SearchProvider, YtsSearchProvider,
};

const HINDI_PATTERNS: &[&str] = &[
    "hindi",
    "hindi dubbed",
    "hindi-dubbed",
    "dual audio",
    "dual-audio",
    "hindi english",
    "hindi-english",
    "bollywood",
    "hindi hd",
    "hindi esub",
    "hindi-dd",
    "hindi cam",
    "hindi hdts",
];

pub fn is_hindi_title(title: &str) -> bool {
    let t = title.to_lowercase();
    HINDI_PATTERNS.iter().any(|p| t.contains(p))
}

pub fn is_dual_audio_title(title: &str) -> bool {
    let t = title.to_lowercase();
    t.contains("dual") && t.contains("audio")
}

pub fn apply_audio_language_filter(
    results: Vec<SearchResult>,
    hindi: bool,
    english: bool,
) -> (Vec<SearchResult>, bool) {
    if !hindi && !english {
        return (results, false);
    }
    if hindi && english {
        let mut dual = Vec::new();
        let mut hindi_only = Vec::new();
        let mut rest = Vec::new();
        for r in results {
            if is_dual_audio_title(&r.title) {
                dual.push(r);
            } else if is_hindi_title(&r.title) {
                hindi_only.push(r);
            } else {
                rest.push(r);
            }
        }
        dual.extend(hindi_only);
        dual.extend(rest);
        return (dual, false);
    }
    if hindi {
        let mut kept: Vec<SearchResult> = results
            .into_iter()
            .filter(|r| is_hindi_title(&r.title))
            .collect();
        kept.sort_by_key(|r| if is_dual_audio_title(&r.title) { 0 } else { 1 });
        return (kept, true);
    }
    let kept = results
        .into_iter()
        .filter(|r| !is_hindi_title(&r.title) || is_dual_audio_title(&r.title))
        .collect();
    (kept, true)
}

pub fn get_query_variations(query: &str) -> Vec<String> {
    let mut variations = Vec::new();
    let q_clean = query.trim();

    let swaps = [
        (r"(?i)\btraveller\b", "traveler"),
        (r"(?i)\btravellers\b", "travelers"),
        (r"(?i)\btravelling\b", "traveling"),
        (r"(?i)\btheatre\b", "theater"),
        (r"(?i)\btheatres\b", "theaters"),
        (r"(?i)\bcolour\b", "color"),
        (r"(?i)\bcolours\b", "colors"),
        (r"(?i)\bneighbour\b", "neighbor"),
        (r"(?i)\bneighbours\b", "neighbors"),
        (r"(?i)\bgrey\b", "gray"),
    ];

    let mut cur = q_clean.to_string();
    for (pat, rep) in swaps {
        if let Ok(re) = Regex::new(pat) {
            if re.is_match(&cur) {
                cur = re.replace_all(&cur, rep).into_owned();
                if !cur.eq_ignore_ascii_case(q_clean) && !variations.contains(&cur) {
                    variations.push(cur.clone());
                }
            }
        }
    }

    let re_punct = Regex::new(r#"['":,\-!?_]"#).unwrap();
    let re_ws = Regex::new(r"\s+").unwrap();
    let no_punct = re_ws
        .replace_all(&re_punct.replace_all(q_clean, " "), " ")
        .trim()
        .to_string();
    if !no_punct.eq_ignore_ascii_case(q_clean) && !variations.contains(&no_punct) {
        variations.push(no_punct);
    }

    if q_clean.to_lowercase().starts_with("the ") {
        let without = q_clean[4..].trim().to_string();
        if !without.is_empty() && !variations.contains(&without) {
            variations.push(without);
        }
    } else {
        let with_the = format!("the {}", q_clean);
        if !variations.contains(&with_the) {
            variations.push(with_the);
        }
    }
    variations
}

#[derive(Clone)]
pub struct SearchService {
    providers: Vec<Arc<dyn SearchProvider>>,
    pub cache_manager: Option<SearchCacheManager>,
}

impl SearchService {
    pub fn new(cache_manager: Option<SearchCacheManager>) -> Self {
        let providers: Vec<Arc<dyn SearchProvider>> = vec![
            Arc::new(PirateBaySearchProvider),
            Arc::new(LimeTorrentsSearchProvider),
            Arc::new(YtsSearchProvider),
            Arc::new(EztvSearchProvider),
            Arc::new(NyaaSearchProvider),
            Arc::new(X1337SearchProvider),
        ];
        Self {
            providers,
            cache_manager,
        }
    }

    async fn query_providers(
        &self,
        term: &str,
        cat: SearchCategory,
        timeout_secs: f64,
    ) -> Vec<SearchResult> {
        let futs = self.providers.iter().map(|p| {
            let prov = Arc::clone(p);
            let q = term.to_string();
            async move { prov.search(&q, cat, timeout_secs).await }
        });
        let lists = join_all(futs).await;
        let mut out = Vec::new();
        let mut seen = HashSet::new();
        for list in lists {
            for item in list {
                let h = item.info_hash.to_lowercase();
                if seen.insert(h) {
                    out.push(item);
                }
            }
        }
        out
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn search(
        &self,
        query: &str,
        category: &str,
        hide_dead: bool,
        limit: usize,
        timeout_secs: f64,
        refresh: bool,
        hindi: bool,
        english: bool,
    ) -> Value {
        let q = query.trim();
        let cat_enum = SearchCategory::from_str(category);

        if q.is_empty() {
            return json!({
                "query": q,
                "category": cat_enum.as_str(),
                "total_found": 0,
                "returned": 0,
                "fallback_applied": false,
                "suggestion": null,
                "is_cached": false,
                "audio_filter": {"hindi": hindi, "english": english, "applied": false},
                "results": [],
            });
        }

        if !refresh && !(hindi || english) {
            if let Some(ref cm) = self.cache_manager {
                if let Some(cached) = cm.get(q, cat_enum.as_str()) {
                    return cached;
                }
            }
        }

        let mut all_results = self.query_providers(q, cat_enum, timeout_secs).await;
        let mut suggestion_used: Option<String> = None;

        if hindi {
            let mut seen_hashes: HashSet<String> = all_results
                .iter()
                .map(|r| r.info_hash.to_lowercase())
                .collect();
            for extra_q in [format!("{} hindi", q), format!("{} dual audio", q)] {
                let extra = self.query_providers(&extra_q, cat_enum, timeout_secs).await;
                for r in extra {
                    if seen_hashes.insert(r.info_hash.to_lowercase()) {
                        all_results.push(r);
                    }
                }
            }
        }

        if all_results.is_empty() {
            for alt in get_query_variations(q) {
                let alt_res = self.query_providers(&alt, cat_enum, timeout_secs).await;
                if !alt_res.is_empty() {
                    all_results = alt_res;
                    suggestion_used = Some(alt);
                    break;
                }
            }
        }

        let today = Utc::now().date_naive();
        for r in &mut all_results {
            if !r.added_date.is_empty() && r.age_days == 0 {
                let d_str = r.added_date.trim();
                let parsed_date = if d_str.len() == 10 {
                    NaiveDate::parse_from_str(d_str, "%Y-%m-%d").ok()
                } else if d_str.len() >= 19 {
                    NaiveDateTime::parse_from_str(&d_str[..19], "%Y-%m-%d %H:%M:%S")
                        .ok()
                        .map(|dt| dt.date())
                } else {
                    None
                };
                if let Some(d) = parsed_date {
                    let days = (today - d).num_days().max(0) as u32;
                    r.age_days = days;
                }
            }
            r.health_score = compute_health_score(r.seeders, r.leechers, r.age_days, r.size_bytes);
        }

        all_results.sort_by(|a, b| {
            b.health_score
                .partial_cmp(&a.health_score)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then_with(|| b.seeders.cmp(&a.seeders))
                .then_with(|| b.leechers.cmp(&a.leechers))
        });

        let total_found = all_results.len();
        let mut fallback_applied = false;

        let mut final_results = if hide_dead {
            let healthy: Vec<SearchResult> = all_results
                .iter()
                .filter(|r| r.health_score >= 0.05)
                .cloned()
                .collect();
            if !healthy.is_empty() {
                healthy
            } else {
                fallback_applied = true;
                all_results
            }
        } else {
            all_results
        };

        let mut audio_applied = false;
        if hindi || english {
            let (filtered, applied) = apply_audio_language_filter(final_results, hindi, english);
            final_results = filtered;
            audio_applied = applied;
        }

        final_results.truncate(limit);
        let results_json: Vec<Value> = final_results.iter().map(|r| r.to_dict()).collect();

        if total_found > 0 && !(hindi || english) {
            if let Some(ref cm) = self.cache_manager {
                cm.set(
                    q,
                    cat_enum.as_str(),
                    &results_json,
                    suggestion_used.as_deref(),
                    total_found,
                    fallback_applied,
                );
            }
        }

        json!({
            "query": q,
            "category": cat_enum.as_str(),
            "total_found": total_found,
            "returned": results_json.len(),
            "fallback_applied": fallback_applied,
            "suggestion": suggestion_used,
            "is_cached": false,
            "audio_filter": {"hindi": hindi, "english": english, "applied": audio_applied},
            "results": results_json,
        })
    }
}
