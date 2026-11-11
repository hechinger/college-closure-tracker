main <- function(cli_args = NULL) {
  args <- if (is.null(cli_args)) commandArgs(trailingOnly = TRUE) else cli_args

  get_script_path <- function() {
    cmd_args <- commandArgs(trailingOnly = FALSE)
    file_arg <- grep("^--file=", cmd_args, value = TRUE)
    if (length(file_arg) > 0) {
      return(normalizePath(sub("^--file=", "", file_arg[[1]]), winslash = "/", mustWork = FALSE))
    }

    frame_files <- vapply(sys.frames(), function(frame) {
      if (!is.null(frame$ofile)) frame$ofile else ""
    }, character(1))
    frame_files <- frame_files[nzchar(frame_files)]
    if (length(frame_files) > 0) {
      return(normalizePath(frame_files[[length(frame_files)]], winslash = "/", mustWork = FALSE))
    }

    normalizePath(file.path(getwd(), "scripts", "publish_closure_tabs_to_google_sheets.R"), winslash = "/", mustWork = FALSE)
  }

  script_path <- get_script_path()
  repo_root <- normalizePath(file.path(dirname(script_path), ".."), winslash = "/", mustWork = FALSE)

  get_arg_value <- function(flag, default = NULL) {
    index <- match(flag, args)
    if (is.na(index) || index == length(args)) {
      return(default)
    }
    args[[index + 1]]
  }

  has_flag <- function(flag) {
    flag %in% args
  }

  ensure_packages <- function(packages) {
    missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly = TRUE)]
    if (length(missing) > 0) {
      tryCatch(
        install.packages(missing, repos = "https://cloud.r-project.org"),
        error = function(error) {
          stop(
            "This script needs these R packages before it can publish to Google Sheets: ",
            paste(missing, collapse = ", "),
            ".\n\n",
            "Automatic package install failed on this machine.\n",
            "Install them manually in R, then rerun the script.\n\n",
            "Original error: ",
            conditionMessage(error),
            call. = FALSE
          )
        }
      )
    }
    still_missing <- packages[!vapply(packages, requireNamespace, logical(1), quietly = TRUE)]
    if (length(still_missing) > 0) {
      stop(
        "These R packages are still missing after the install step: ",
        paste(still_missing, collapse = ", "),
        ".\nInstall them manually in R, then rerun the script.",
        call. = FALSE
      )
    }
    invisible(lapply(packages, library, character.only = TRUE))
  }

  ensure_packages(c("googlesheets4", "readr"))

  derived_dir <- get_arg_value("--derived-dir", file.path(repo_root, "derived"))
  sheet_url <- get_arg_value("--sheet", Sys.getenv("GOOGLE_SHEET_URL", unset = ""))
  auth_json <- get_arg_value("--auth-json", Sys.getenv("GOOGLE_APPLICATION_CREDENTIALS", unset = ""))
  auth_cache <- get_arg_value("--auth-cache", file.path(repo_root, ".auth", "googlesheets4"))
  auth_email <- get_arg_value("--email", Sys.getenv("GOOGLE_AUTH_EMAIL", unset = ""))
  verbose <- has_flag("--verbose")

  if (!nzchar(sheet_url)) {
    stop(
      "No Google Sheet target was provided.\n",
      "Pass --sheet <Google Sheet URL> or set GOOGLE_SHEET_URL before running this script."
    )
  }

  required_tabs <- c(
    "closure_status_tracker_matches",
    "running_closures",
    "main_campus_closures",
    "branch_campus_closures",
    "mergers_consolidations",
    "private_sector_federal_main_closures"
  )

  required_files <- file.path(derived_dir, paste0(required_tabs, ".csv"))
  missing_files <- required_files[!file.exists(required_files)]
  if (length(missing_files) > 0) {
    stop(
      "publish_closure_tabs_to_google_sheets.R is missing derived CSV files.\n\n",
      "Run scripts/build_closure_outputs.py first, then rerun this script.\n\n",
      "Missing files:\n- ",
      paste(missing_files, collapse = "\n- ")
    )
  }

  auth_cache <- normalizePath(auth_cache, winslash = "/", mustWork = FALSE)
  dir.create(auth_cache, recursive = TRUE, showWarnings = FALSE)
  cache_has_tokens <- length(list.files(auth_cache, all.files = TRUE, no.. = TRUE, recursive = TRUE)) > 0
  email_value <- if (nzchar(auth_email)) auth_email else TRUE

  if (nzchar(auth_json)) {
    if (!file.exists(auth_json)) {
      stop("The file passed to --auth-json does not exist: ", auth_json)
    }
    if (verbose) {
      message("Authenticating with Google credentials file: ", auth_json)
    }
    googlesheets4::gs4_auth(path = auth_json, scopes = "spreadsheets", cache = FALSE)
  } else {
    if (!interactive() && !cache_has_tokens) {
      stop(
        "This script is running in a non-interactive session and no cached Google token was found.\n\n",
        "To set up local command-line publishing, run this script once from interactive R or RStudio so Google can open a login window and save a token cache.\n\n",
        "Suggested interactive setup:\n",
        "  setwd('", repo_root, "')\n",
        "  source('scripts/publish_closure_tabs_to_google_sheets.R')\n",
        "  main(c('--sheet', '", sheet_url, "'))\n\n",
        "That interactive run will save the token in: ", auth_cache, "\n",
        "After that, you can rerun this script from PowerShell with Rscript and it will reuse the cached token.",
        call. = FALSE
      )
    }

    if (verbose) {
      if (cache_has_tokens) {
        message("Authenticating with cached Google token in: ", auth_cache)
      } else {
        message("Starting interactive Google login and saving token cache in: ", auth_cache)
      }
    }

    googlesheets4::gs4_auth(
      email = email_value,
      scopes = "spreadsheets",
      cache = auth_cache
    )
  }

  for (tab_name in required_tabs) {
    csv_path <- file.path(derived_dir, paste0(tab_name, ".csv"))
    if (verbose) {
      message("Uploading ", tab_name, " from ", csv_path)
    }
    data <- readr::read_csv(csv_path, show_col_types = FALSE, progress = FALSE)
    googlesheets4::sheet_write(data = data, ss = sheet_url, sheet = tab_name)
  }

  sheet_info <- tryCatch(googlesheets4::gs4_get(sheet_url), error = function(e) NULL)
  if (!is.null(sheet_info) && "spreadsheet_url" %in% names(sheet_info)) {
    message("Published closure tabs to: ", sheet_info$spreadsheet_url[[1]])
  } else {
    message("Published closure tabs to: ", sheet_url)
  }
  message("Tabs refreshed: ", paste(required_tabs, collapse = ", "))
}

if (sys.nframe() == 0) {
  main()
}
