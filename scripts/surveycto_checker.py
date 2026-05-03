#!/usr/bin/env python3
"""
SurveyCTO Form Checker

Validates XLSForm files for common errors:
- Expression syntax errors (unbalanced parentheses, unclosed ${} references, unclosed quotes)
- References to non-existent fields in relevance, choice_filter, calculation, and constraint expressions
- Undefined choice lists
- Missing required columns
- Typos in field names and labels
- Missing constraint messages
- Integer fields without constraints
- Numeric fields (integer/decimal) without -999 refuse option
- Calculate fields without calculation formulas
- Missing Hindi translations
- Naming convention issues
- select_multiple questions with 'other' option but no specify field
- select_multiple questions with exclusive options (don't know, refuse, nothing, etc.) missing constraints
- Conditional formatting rules (type-based color coding) are preserved
- Cell formatting (red text for unverified translations) is preserved
- Version formula in settings sheet is evaluated

Usage:
    python surveycto_checker.py <path_to_xlsform.xlsx>
    python surveycto_checker.py  # checks ai_health_pilot_baseline.xlsx by default
"""

import pandas as pd
import re
import sys
import subprocess
from pathlib import Path
import openpyxl


class SurveyCTOChecker:
    """Validates SurveyCTO XLSForm files for common errors."""

    def __init__(self, file_path):
        self.file_path = Path(file_path)
        self.survey_df = None
        self.choices_df = None
        self.settings_df = None
        self.errors = []
        self.warnings = []

    def load_form(self):
        """Load the XLSForm file."""
        try:
            self.survey_df = pd.read_excel(self.file_path, sheet_name='survey')
            self.choices_df = pd.read_excel(self.file_path, sheet_name='choices')
            try:
                self.settings_df = pd.read_excel(self.file_path, sheet_name='settings')
            except ValueError:
                self.warnings.append("No 'settings' sheet found (optional)")

            # SurveyCTO uses 'value' for choice identifiers; XLSForm standard uses 'name'.
            # Accept either by renaming 'value' -> 'name' when 'name' is absent.
            if 'name' not in self.choices_df.columns and 'value' in self.choices_df.columns:
                self.choices_df = self.choices_df.rename(columns={'value': 'name'})

            # XLSForm spec is 'constraint message' (space); some teams use 'constraint_message'
            # (underscore). Internally the checker uses the underscore form, so alias the spec
            # name to it when the underscore form isn't present.
            if ('constraint_message' not in self.survey_df.columns
                    and 'constraint message' in self.survey_df.columns):
                self.survey_df = self.survey_df.rename(
                    columns={'constraint message': 'constraint_message'})

            # Filter out disabled rows from survey (preserve original indices for row reporting)
            if 'disabled' in self.survey_df.columns:
                disabled_count = (self.survey_df['disabled'].astype(str).str.lower() == 'yes').sum()
                self.survey_df = self.survey_df[
                    self.survey_df['disabled'].astype(str).str.lower() != 'yes'
                ]
                if disabled_count > 0:
                    print(f"Filtered out {disabled_count} disabled row(s)")

            return True
        except Exception as e:
            self.errors.append(f"Failed to load file: {e}")
            return False

    def check_field_references(self):
        """Check for references to non-existent fields."""
        print("\n=== Checking Field References ===")

        # Get all existing field names
        existing_fields = set(self.survey_df['name'].dropna().astype(str))
        print(f"Found {len(existing_fields)} defined fields")

        # Columns that may contain field references
        # Note: label and hint columns can also contain ${field} references for piping
        reference_columns = ['relevance', 'choice_filter', 'calculation', 'constraint',
                            'constraint_message', 'repeat_count', 'default',
                            'label', 'label:Hindi', 'hint', 'hint:Hindi']

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_name = row.get('name', f'Row {idx}')

            for col in reference_columns:
                if pd.notna(row.get(col)):
                    expression = str(row[col])

                    # Find all ${field_name} references
                    references = re.findall(r'\$\{([^}]+)\}', expression)

                    for ref in references:
                        # Clean up the reference (remove function calls, indexing, etc.)
                        base_ref = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)', ref)
                        if base_ref:
                            base_field = base_ref.group(1)
                            if base_field not in existing_fields:
                                issues.append({
                                    'row': idx + 2,  # +2 for Excel row (1-indexed + header)
                                    'field': field_name,
                                    'column': col,
                                    'missing_ref': base_field,
                                    'expression': expression
                                })

        if issues:
            print(f"\n❌ Found {len(issues)} reference(s) to non-existent fields:\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: '{issue['field']}' in column '{issue['column']}'\n"
                           f"    References non-existent field: ${{{issue['missing_ref']}}}\n"
                           f"    Expression: {issue['expression']}\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All field references are valid")

        return len(issues) == 0

    def check_choices_field_references(self):
        """Check for references to non-existent fields in choices sheet labels.

        Choice labels can contain ${field_name} references (e.g., ${adult_label_1})
        that are piped from survey fields. This checks that all such references
        point to fields that exist in the survey.
        """
        print("\n=== Checking Choices Sheet Field References ===")

        # Get all existing field names from survey
        existing_fields = set(self.survey_df['name'].dropna().astype(str))

        issues = []

        # Check label columns in choices sheet
        label_columns = [col for col in self.choices_df.columns if col.startswith('label')]

        for idx, row in self.choices_df.iterrows():
            list_name = row.get('list_name', '')
            choice_name = row.get('name', '')

            for col in label_columns:
                if pd.notna(row.get(col)):
                    label = str(row[col])

                    # Find all ${field_name} references
                    references = re.findall(r'\$\{([^}]+)\}', label)

                    for ref in references:
                        # Clean up the reference (remove function calls, indexing, etc.)
                        base_ref = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)', ref)
                        if base_ref:
                            base_field = base_ref.group(1)
                            if base_field not in existing_fields:
                                issues.append({
                                    'row': idx + 2,  # +2 for Excel row (1-indexed + header)
                                    'list_name': list_name,
                                    'choice_name': choice_name,
                                    'column': col,
                                    'missing_ref': base_field,
                                    'label': label[:50]
                                })

        if issues:
            # Group by missing field for cleaner output
            missing_fields = set(issue['missing_ref'] for issue in issues)
            print(f"\n❌ Found {len(issues)} reference(s) to {len(missing_fields)} non-existent field(s) in choices:\n")

            for field in sorted(missing_fields):
                field_issues = [i for i in issues if i['missing_ref'] == field]
                lists_affected = set(i['list_name'] for i in field_issues)
                error_msg = (f"  Missing field: ${{{field}}}\n"
                           f"    Used in choice lists: {', '.join(sorted(lists_affected))}\n"
                           f"    Rows: {', '.join(str(i['row']) for i in field_issues[:5])}"
                           f"{'...' if len(field_issues) > 5 else ''}\n")
                print(error_msg)
                self.errors.append(f"Choices sheet references non-existent field: ${{{field}}}")
        else:
            print("✅ All field references in choices are valid")

        return len(issues) == 0

    def check_choice_lists(self):
        """Check for references to undefined choice lists."""
        print("\n=== Checking Choice Lists ===")

        # Get all defined choice lists
        defined_lists = set(self.choices_df['list_name'].dropna().unique())
        print(f"Found {len(defined_lists)} defined choice lists")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', ''))
            field_name = row.get('name', f'Row {idx}')

            # Check select_one and select_multiple types
            if field_type.startswith('select_one '):
                list_name = field_type.replace('select_one ', '').strip()
                if list_name and list_name not in defined_lists:
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'type': field_type,
                        'missing_list': list_name
                    })

            elif field_type.startswith('select_multiple '):
                list_name = field_type.replace('select_multiple ', '').strip()
                if list_name and list_name not in defined_lists:
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'type': field_type,
                        'missing_list': list_name
                    })

        if issues:
            print(f"\n❌ Found {len(issues)} reference(s) to undefined choice lists:\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: '{issue['field']}' type '{issue['type']}'\n"
                           f"    References undefined list: '{issue['missing_list']}'\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All choice lists are defined")

        return len(issues) == 0

    def check_required_columns(self):
        """Check for required columns in survey and choices sheets."""
        print("\n=== Checking Required Columns ===")

        required_survey_cols = ['type', 'name']
        required_choices_cols = ['list_name', 'name', 'label']

        missing_survey = [col for col in required_survey_cols if col not in self.survey_df.columns]
        missing_choices = [col for col in required_choices_cols if col not in self.choices_df.columns]

        if missing_survey:
            error_msg = f"Survey sheet missing required columns: {missing_survey}"
            print(f"❌ {error_msg}")
            self.errors.append(error_msg)

        if missing_choices:
            error_msg = f"Choices sheet missing required columns: {missing_choices}"
            print(f"❌ {error_msg}")
            self.errors.append(error_msg)

        if not missing_survey and not missing_choices:
            print("✅ All required columns present")
            return True

        return False

    def check_duplicate_names(self):
        """Check for duplicate field names."""
        print("\n=== Checking for Duplicate Field Names ===")

        # Get all field names (excluding NaN and structural types)
        field_names = self.survey_df[
            self.survey_df['type'].notna() &
            ~self.survey_df['type'].str.contains('group|repeat', case=False, na=False)
        ]['name'].dropna()

        duplicates = field_names[field_names.duplicated()].unique()

        if len(duplicates) > 0:
            print(f"\n❌ Found {len(duplicates)} duplicate field name(s):\n")
            for dup in duplicates:
                rows = self.survey_df[self.survey_df['name'] == dup].index + 2
                error_msg = f"  Field '{dup}' appears in rows: {list(rows)}"
                print(error_msg)
                self.errors.append(error_msg)
            return False
        else:
            print("✅ No duplicate field names found")
            return True

    def check_required_fields(self):
        """Check that all question fields have required = yes.

        Non-question types (notes, groups, repeats, metadata, calculations) are excluded.
        """
        print("\n=== Checking Required Fields ===")

        # Types that are NOT questions and should be excluded from this check
        non_question_types = {
            'note', 'calculate', 'deviceid', 'subscriberid', 'simserial',
            'phonenumber', 'username', 'start', 'end', 'caseid', 'geopoint',
            'begin group', 'end group', 'begin repeat', 'end repeat',
            'begin_group', 'end_group', 'begin_repeat', 'end_repeat'
        }

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).strip().lower()
            field_name = row.get('name', f'Row {idx}')
            required_val = str(row.get('required', '')).strip().lower()

            # Skip if no type or if it's a non-question type
            if not field_type or pd.isna(row.get('type')):
                continue

            # Check if it starts with a non-question type (handles "begin group" etc.)
            is_non_question = False
            for nq_type in non_question_types:
                if field_type == nq_type or field_type.startswith(nq_type):
                    is_non_question = True
                    break

            if is_non_question:
                continue

            # This is a question type - check if required = yes
            if required_val != 'yes':
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'type': field_type,
                    'required': required_val if required_val else '(blank)'
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} question(s) without required=yes:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' (type: {issue['type']})\n"
                             f"    required = {issue['required']}\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All questions have required=yes")

        return len(issues) == 0

    def check_other_specify_fields(self):
        """Check that 'other (specify)' choices have corresponding specify fields.

        This check looks for choice lists where 'other' requires a follow-up text field.
        It uses multiple heuristics to match various naming conventions.
        """
        print("\n=== Checking 'Other Specify' Fields ===")

        issues = []

        # Get all existing field names for pattern matching
        existing_fields = set(self.survey_df['name'].dropna().astype(str))

        # Find choice lists with 'other' option that requires specification
        # Look for 'other' choices with labels containing "specify" or similar
        other_choices = self.choices_df[
            self.choices_df['name'].astype(str).str.lower() == 'other'
        ]

        # Determine which lists need a specify field based on the label
        lists_needing_specify = set()
        for _, choice_row in other_choices.iterrows():
            list_name = choice_row.get('list_name', '')
            label = str(choice_row.get('label', '')).lower()
            # Check if label indicates specification is needed
            if 'specify' in label or 'बताएं' in label:
                lists_needing_specify.add(list_name)

        print(f"Found {len(lists_needing_specify)} choice list(s) with 'other (specify)' option")

        # Find all fields using these lists
        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', ''))
            field_name = str(row.get('name', ''))

            # Extract list name from select_one/select_multiple
            if field_type.startswith('select_one '):
                list_name = field_type.replace('select_one ', '').strip()
            elif field_type.startswith('select_multiple '):
                list_name = field_type.replace('select_multiple ', '').strip()
            else:
                continue

            # Only check lists that need specify fields
            if list_name not in lists_needing_specify:
                continue

            # Generate possible "other" field name patterns
            # Pattern 1: {field_name}_other (e.g., s4_provider_type_other)
            # Pattern 2: {section}{num}_other (e.g., s4_other from s4_provider_type)
            # Pattern 3: {section}{num}_{first_word}_other (e.g., s4_provider_other)
            # Pattern 4: {field_name}_other_specify
            # Pattern 5: {section}{num}_other_specify (e.g., v1_other_specify)
            # Pattern 6: {field_name}_other_text
            possible_patterns = []

            # Pattern 1: exact match
            possible_patterns.append(f"{field_name}_other")

            # Extract section prefix (e.g., 's4', 'v1', 't3')
            section_match = re.match(r'^([a-z]+\d+[a-z]?)_', field_name)
            if section_match:
                section_prefix = section_match.group(1)
                # Pattern 2: section + _other
                possible_patterns.append(f"{section_prefix}_other")
                # Pattern 5: section + _other_specify
                possible_patterns.append(f"{section_prefix}_other_specify")

                # Pattern 3: section + first part of name + _other
                remaining = field_name[len(section_prefix) + 1:]  # Skip prefix and underscore
                if '_' in remaining:
                    first_part = remaining.split('_')[0]
                    possible_patterns.append(f"{section_prefix}_{first_part}_other")

            # Pattern 4 & 6: variations with _specify or _text suffix
            possible_patterns.append(f"{field_name}_other_specify")
            possible_patterns.append(f"{field_name}_other_text")

            # Check if any pattern matches an existing field
            found_match = False
            for pattern in possible_patterns:
                if pattern in existing_fields:
                    found_match = True
                    break

            if not found_match:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'tried_patterns': possible_patterns[:3],  # Show first 3 patterns
                    'list': list_name
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} field(s) with 'other (specify)' choice but no specify field:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' (list: '{issue['list']}')\n"
                             f"    Tried patterns: {', '.join(issue['tried_patterns'])}\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All 'other (specify)' choices have specify fields")

        return len(issues) == 0

    def check_expression_syntax(self):
        """Check for syntax errors in relevance, calculation, and constraint expressions.

        Validates:
        - Balanced parentheses
        - Properly closed ${} field references
        - Balanced quotes (single and double)
        - SurveyCTO parser-sensitive spaced comparison operators
        """
        print("\n=== Checking Expression Syntax ===")

        # Columns that contain expressions
        expression_columns = ['relevance', 'calculation', 'constraint', 'choice_filter',
                              'repeat_count', 'default']

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_name = row.get('name', f'Row {idx}')

            for col in expression_columns:
                if pd.notna(row.get(col)):
                    expression = str(row[col])
                    syntax_errors = self._check_expression(expression)

                    for error in syntax_errors:
                        issues.append({
                            'row': idx + 2,
                            'field': field_name,
                            'column': col,
                            'error': error,
                            'expression': expression
                        })

        if issues:
            print(f"\n❌ Found {len(issues)} expression syntax error(s):\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: '{issue['field']}' in column '{issue['column']}'\n"
                           f"    {issue['error']}\n"
                           f"    Expression: {issue['expression']}\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All expressions have valid syntax")

        return len(issues) == 0

    def _check_expression(self, expression):
        """Check a single expression for syntax errors.

        Returns a list of error messages (empty if no errors).
        """
        errors = []

        # Check 1: Balanced parentheses
        paren_count = 0
        for i, char in enumerate(expression):
            if char == '(':
                paren_count += 1
            elif char == ')':
                paren_count -= 1
                if paren_count < 0:
                    errors.append(f"Unmatched closing parenthesis ')' at position {i}")
                    break

        if paren_count > 0:
            errors.append(f"Unclosed parenthesis - {paren_count} opening '(' without matching ')'")

        # Check 2: Properly closed ${} references
        i = 0
        while i < len(expression):
            if expression[i:i+2] == '${':
                # Find closing brace
                close_pos = expression.find('}', i + 2)
                if close_pos == -1:
                    errors.append(f"Unclosed field reference '${{' at position {i}")
                    break
                i = close_pos + 1
            else:
                i += 1

        # Check 3: Balanced quotes (but handle escaped quotes and mixed usage)
        # SurveyCTO uses single quotes for strings, double quotes when string contains single quotes
        in_single_quote = False
        in_double_quote = False

        for i, char in enumerate(expression):
            if char == "'" and not in_double_quote:
                in_single_quote = not in_single_quote
            elif char == '"' and not in_single_quote:
                in_double_quote = not in_double_quote

        if in_single_quote:
            errors.append("Unclosed single quote '")
        if in_double_quote:
            errors.append('Unclosed double quote "')

        # Check 4: SurveyCTO rejects split comparison operators like ". > = 0".
        if re.search(r'(?:>|<|!)\s+=', expression):
            errors.append("Invalid spaced comparison operator; use >=, <=, or != without spaces")

        return errors

    def check_upload_parser_blockers(self):
        """Catch parser-level issues that SurveyCTO rejects at upload time.

        These are stricter than the local semantic checks above:
        - Spacer rows with no type/name/label but with relevance/calculation/etc.
        - Self-referential relevance/calculation expressions, which create XPath
          dependency cycles in SurveyCTO.
        """
        print("\n=== Checking SurveyCTO Upload Parser Blockers ===")

        expression_columns = ['relevance', 'calculation', 'constraint', 'choice_filter',
                              'repeat_count', 'default']
        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = row.get('type', '')
            field_name = row.get('name', '')
            label = row.get('label', '')

            has_identity = any(
                pd.notna(v) and str(v).strip()
                for v in (field_type, field_name, label)
            )
            active_expressions = [
                col for col in expression_columns
                if pd.notna(row.get(col)) and str(row.get(col)).strip()
            ]
            if not has_identity and active_expressions:
                issues.append({
                    'row': idx + 2,
                    'field': '(blank row)',
                    'problem': (
                        "Row has no type/name/label but has expression(s) in "
                        f"{', '.join(active_expressions)}"
                    ),
                })

            if pd.notna(field_name) and str(field_name).strip():
                name = str(field_name).strip()
                for col in ('relevance', 'calculation'):
                    expression = row.get(col)
                    if pd.isna(expression) or not str(expression).strip():
                        continue
                    refs = re.findall(r'\$\{([^}]+)\}', str(expression))
                    for ref in refs:
                        base_ref = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)', ref)
                        if base_ref and base_ref.group(1) == name:
                            issues.append({
                                'row': idx + 2,
                                'field': name,
                                'problem': (
                                    f"Self-reference in {col}: {str(expression)}"
                                ),
                            })

        if issues:
            print(f"\n❌ Found {len(issues)} SurveyCTO upload parser blocker(s):\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: '{issue['field']}'\n"
                             f"    {issue['problem']}\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ No SurveyCTO upload parser blockers found")

        return len(issues) == 0

    def check_typos(self):
        """Check for common typos in field names and labels."""
        print("\n=== Checking for Typos ===")

        # Common typos to check for
        typos = [
            ('enumnerator', 'enumerator'),
            ('heatlh', 'health'),
            ('helath', 'health'),
            ('symtoms', 'symptoms'),
            ('sympton', 'symptom'),
            ('speciy', 'specify'),
            ('provder', 'provider'),
            ('repondent', 'respondent'),
            ('hosptial', 'hospital'),
            ('vilalge', 'village'),
            ('distirct', 'district'),
            ('sevrity', 'severity'),
            ('severeity', 'severity'),
        ]

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_name = str(row.get('name', '')).lower()
            label = str(row.get('label', '')).lower()
            field_name_orig = row.get('name', f'Row {idx}')

            for typo, correct in typos:
                # Check field name
                if typo in field_name:
                    issues.append({
                        'row': idx + 2,
                        'field': field_name_orig,
                        'location': 'field name',
                        'typo': typo,
                        'correct': correct
                    })
                # Check label
                if typo in label:
                    issues.append({
                        'row': idx + 2,
                        'field': field_name_orig,
                        'location': 'label',
                        'typo': typo,
                        'correct': correct
                    })

        if issues:
            print(f"\n⚠️  Found {len(issues)} potential typo(s):\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' in {issue['location']}\n"
                             f"    Found '{issue['typo']}' - should be '{issue['correct']}'?\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ No common typos found")

        return len(issues) == 0

    def check_missing_constraint_messages(self):
        """Check that fields with constraints have constraint messages."""
        print("\n=== Checking Constraint Messages ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_name = row.get('name', f'Row {idx}')
            constraint = row.get('constraint', '')
            constraint_msg = row.get('constraint_message', '')

            if pd.notna(constraint) and str(constraint).strip():
                if pd.isna(constraint_msg) or not str(constraint_msg).strip():
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'constraint': str(constraint)
                    })

        if issues:
            print(f"\n⚠️  Found {len(issues)} field(s) with constraint but no message:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}'\n"
                             f"    constraint: {issue['constraint']}\n"
                             f"    Missing constraint_message\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All constraints have messages")

        return len(issues) == 0

    def check_integer_constraints(self):
        """Check that integer fields have constraints."""
        print("\n=== Checking Integer Constraints ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).strip().lower()
            field_name = row.get('name', f'Row {idx}')
            constraint = row.get('constraint', '')
            label = str(row.get('label', ''))[:50]

            if field_type == 'integer':
                if pd.isna(constraint) or not str(constraint).strip():
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'label': label
                    })

        if issues:
            print(f"\n⚠️  Found {len(issues)} integer field(s) without constraints:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}'\n"
                             f"    Label: {issue['label']}\n"
                             f"    Consider adding range validation\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All integer fields have constraints")

        return len(issues) == 0

    def check_calculate_fields(self):
        """Check that calculate fields have a calculation formula.

        Fields with type 'calculate' or 'calculate_here' must have a non-empty
        'calculation' column, otherwise they serve no purpose and are likely errors.

        For calculate_here fields (timing checkpoints), the calculation should
        typically be something like once(duration()) or once(format-date-time(now(), ...)).
        """
        print("\n=== Checking Calculate Fields ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).strip().lower()
            field_name = row.get('name', f'Row {idx}')
            calculation = row.get('calculation', '')

            # Check both 'calculate' and 'calculate_here' types
            if field_type in ['calculate', 'calculate_here']:
                if pd.isna(calculation) or not str(calculation).strip():
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'type': field_type
                    })

        if issues:
            print(f"\n❌ Found {len(issues)} calculate field(s) with empty calculation:\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: '{issue['field']}' (type: {issue['type']})\n"
                           f"    Missing calculation formula\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All calculate fields have formulas")

        return len(issues) == 0

    def check_hindi_translations(self):
        """Check for missing Hindi translations on questions."""
        print("\n=== Checking Hindi Translations ===")

        if 'label:Hindi' not in self.survey_df.columns:
            print("ℹ️  No 'label:Hindi' column found (single language form)")
            return True

        # Question types that need labels
        question_prefixes = ['text', 'integer', 'decimal', 'select_one', 'select_multiple',
                            'geopoint', 'image', 'date', 'time']

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).lower()
            field_name = row.get('name', f'Row {idx}')
            label = row.get('label', '')
            label_hindi = row.get('label:Hindi', '')

            # Check if this is a question type
            is_question = any(field_type.startswith(prefix) for prefix in question_prefixes)

            if is_question and pd.notna(label) and str(label).strip():
                if pd.isna(label_hindi) or not str(label_hindi).strip():
                    issues.append({
                        'row': idx + 2,
                        'field': field_name,
                        'label': str(label)[:50]
                    })

        if issues:
            print(f"\n⚠️  Found {len(issues)} question(s) missing Hindi translation:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}'\n"
                             f"    English: {issue['label']}\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All questions have Hindi translations")

        return len(issues) == 0

    def check_select_multiple_other(self):
        """Check that select_multiple questions with 'other' option have specify fields.

        Unlike check_other_specify_fields which only checks choices labeled 'other (specify)',
        this checks ALL select_multiple questions that have any 'other' choice option.
        """
        print("\n=== Checking select_multiple 'Other' Fields ===")

        issues = []

        # Get all existing field names for pattern matching
        existing_fields = set(self.survey_df['name'].dropna().astype(str))

        # Find all choice lists that have an 'other' option (any 'other', not just 'other specify')
        lists_with_other = set(
            self.choices_df[
                self.choices_df['name'].astype(str).str.lower() == 'other'
            ]['list_name'].dropna().unique()
        )

        # Find all select_multiple fields using lists with 'other' option
        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', ''))
            field_name = str(row.get('name', ''))

            # Only check select_multiple
            if not field_type.startswith('select_multiple '):
                continue

            list_name = field_type.replace('select_multiple ', '').strip()

            # Only check if this list has an 'other' option
            if list_name not in lists_with_other:
                continue

            # Generate possible "other" field name patterns
            possible_patterns = []

            # Pattern 1: {field_name}_other
            possible_patterns.append(f"{field_name}_other")

            # Extract section prefix (e.g., 's4', 'v1', 't3')
            section_match = re.match(r'^([a-z]+\d+[a-z]?)_', field_name)
            if section_match:
                section_prefix = section_match.group(1)
                # Pattern 2: section + _other
                possible_patterns.append(f"{section_prefix}_other")
                # Pattern 3: section + _other_specify
                possible_patterns.append(f"{section_prefix}_other_specify")

                # Pattern 4: section + first part of name + _other
                remaining = field_name[len(section_prefix) + 1:]
                if '_' in remaining:
                    first_part = remaining.split('_')[0]
                    possible_patterns.append(f"{section_prefix}_{first_part}_other")

            # Additional patterns
            possible_patterns.append(f"{field_name}_other_specify")
            possible_patterns.append(f"{field_name}_other_text")

            # Check if any pattern matches an existing field
            found_match = False
            for pattern in possible_patterns:
                if pattern in existing_fields:
                    found_match = True
                    break

            if not found_match:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'list': list_name,
                    'tried_patterns': possible_patterns[:3]
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} select_multiple field(s) with 'other' option but no specify field:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' (list: '{issue['list']}')\n"
                             f"    Tried patterns: {', '.join(issue['tried_patterns'])}\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All select_multiple fields with 'other' option have specify fields")

        return len(issues) == 0

    def check_select_multiple_exclusive(self):
        """Check that select_multiple questions with exclusive options have constraints.

        Certain options like 'Don't know' (-97), 'Refuse to answer' (-98), 'Nothing',
        'Not sick', etc. should be exclusive - they cannot be selected along with other options.
        This check verifies that select_multiple questions with such options have appropriate
        constraints like: not(selected(., '-97')) or count-selected(.) = 1
        """
        print("\n=== Checking select_multiple Exclusive Options ===")

        issues = []

        # Patterns that indicate exclusive options
        # Use exact match for choice names, substring match for labels
        # Short patterns like 'na', 'dk' only match as exact choice names to avoid false positives
        exact_name_patterns = [
            '-97', '-98', '-99',  # Numeric codes for dk/refuse/na
            'dk', 'na',  # Short codes - only match exactly
            'none', 'nothing',
            'not_sick',
            'dont_know', 'dont_remember',
        ]

        # These patterns can match as substrings in labels
        label_patterns = [
            "don't know", "don't remember",
            'refuse to answer', 'declined to answer',
            'not applicable',
            'none of the above', 'none of these',
        ]

        # Build a map of choice lists to their exclusive options
        list_exclusive_opts = {}
        for list_name in self.choices_df['list_name'].dropna().unique():
            list_choices = self.choices_df[self.choices_df['list_name'] == list_name]
            exclusive_opts = []
            for _, choice_row in list_choices.iterrows():
                choice_name = str(choice_row.get('name', '')).lower()
                choice_label = str(choice_row.get('label', '')).lower() if pd.notna(choice_row.get('label')) else ''

                is_exclusive = False

                # Check exact match for name patterns
                for pattern in exact_name_patterns:
                    if choice_name == pattern:
                        is_exclusive = True
                        break

                # Check substring match for label patterns (more specific phrases)
                if not is_exclusive:
                    for pattern in label_patterns:
                        if pattern in choice_label:
                            is_exclusive = True
                            break

                if is_exclusive:
                    exclusive_opts.append(choice_row.get('name'))

            if exclusive_opts:
                list_exclusive_opts[list_name] = exclusive_opts

        # Check each select_multiple question
        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', ''))
            field_name = str(row.get('name', ''))
            constraint = str(row.get('constraint', '')) if pd.notna(row.get('constraint')) else ''

            if not field_type.startswith('select_multiple '):
                continue

            list_name = field_type.replace('select_multiple ', '').strip()

            # Check if this list has exclusive options
            if list_name not in list_exclusive_opts:
                continue

            exclusive_opts = list_exclusive_opts[list_name]

            # Check if constraint handles each exclusive option
            missing_constraints = []
            for opt in exclusive_opts:
                opt_str = str(opt)
                # Look for patterns like: not(selected(., 'opt')) or count-selected(.) = 1
                # or: selected(., 'opt') ... count-selected
                if opt_str not in constraint:
                    missing_constraints.append(opt_str)

            if missing_constraints:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'list': list_name,
                    'exclusive_opts': exclusive_opts,
                    'missing': missing_constraints,
                    'current_constraint': constraint if constraint else '(none)'
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} select_multiple field(s) missing exclusive option constraints:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' (list: '{issue['list']}')\n"
                             f"    Exclusive options: {issue['exclusive_opts']}\n"
                             f"    Missing constraint for: {issue['missing']}\n"
                             f"    Current constraint: {issue['current_constraint']}\n"
                             f"    Suggested: (not(selected(., '{issue['missing'][0]}')) or count-selected(.) = 1)\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All select_multiple fields with exclusive options have constraints")

        return len(issues) == 0

    def check_numeric_refuse_option(self):
        """Check that numeric fields (integer/decimal) have -999 refuse option.

        All numeric fields should allow respondents to refuse answering by entering -999.
        The constraint should include 'or . = -999' or similar pattern.
        """
        print("\n=== Checking Numeric Refuse Option (-999) ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).strip().lower()
            field_name = row.get('name', f'Row {idx}')
            constraint = str(row.get('constraint', '')) if pd.notna(row.get('constraint')) else ''

            # Only check integer and decimal types
            if field_type not in ['integer', 'decimal']:
                continue

            # Check if -999 is in the constraint
            if '-999' not in constraint:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'type': field_type,
                    'constraint': constraint if constraint else '(no constraint)'
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} numeric field(s) without -999 refuse option:\n")
            for issue in issues:
                warning_msg = (f"  Row {issue['row']}: '{issue['field']}' (type: {issue['type']})\n"
                             f"    Constraint: {issue['constraint']}\n"
                             f"    Add: or . = -999\n")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All numeric fields have -999 refuse option")

        return len(issues) == 0

    def check_blank_names(self):
        """Check that all rows with a type also have a non-blank name.

        SurveyCTO requires every field (including metadata types like subscriberid)
        to have a non-whitespace name. A space or empty name causes upload errors like:
        "Question or group with no name [row : N]"
        """
        print("\n=== Checking for Blank/Missing Names ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_type = row.get('type', '')
            field_name = row.get('name', '')

            # Skip rows with no type
            if pd.isna(field_type) or not str(field_type).strip():
                continue

            # Check if name is missing, NaN, or whitespace-only
            if pd.isna(field_name) or not str(field_name).strip():
                issues.append({
                    'row': idx + 2,
                    'type': str(field_type).strip(),
                    'name': repr(field_name) if not pd.isna(field_name) else '(empty)'
                })

        if issues:
            print(f"\n❌ Found {len(issues)} row(s) with type but blank/missing name:\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: type='{issue['type']}', name={issue['name']}\n"
                           f"    Every row with a type must have a non-blank name\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All typed rows have names")

        return len(issues) == 0

    def check_empty_groups(self):
        """Check that groups and repeats have at least one enabled child.

        After filtering disabled rows, a group may have no children left.
        SurveyCTO rejects these with: "Group has no children! Group: <name>"
        This check handles nested groups correctly.
        """
        print("\n=== Checking for Empty Groups ===")

        issues = []

        # Build a list of (type, name) tuples preserving order
        rows = []
        for idx, row in self.survey_df.iterrows():
            field_type = str(row.get('type', '')).strip().lower() if pd.notna(row.get('type')) else ''
            field_name = str(row.get('name', '')).strip() if pd.notna(row.get('name')) else ''
            rows.append((idx + 2, field_type, field_name))  # Excel row, type, name

        # Use a stack to track group nesting and child counts
        # Each stack entry: (excel_row, group_name, child_count)
        stack = []

        for excel_row, field_type, field_name in rows:
            if field_type in ('begin group', 'begin_group', 'begin repeat', 'begin_repeat'):
                stack.append((excel_row, field_name, 0))
            elif field_type in ('end group', 'end_group', 'end repeat', 'end_repeat'):
                if stack:
                    begin_row, group_name, child_count = stack.pop()
                    if child_count == 0:
                        issues.append({
                            'row': begin_row,
                            'name': group_name,
                            'end_row': excel_row
                        })
                    # The group itself counts as a child of its parent
                    if stack:
                        parent = stack[-1]
                        stack[-1] = (parent[0], parent[1], parent[2] + 1)
            else:
                # Non-structural row — increment child count of current group
                if stack and field_type:
                    parent = stack[-1]
                    stack[-1] = (parent[0], parent[1], parent[2] + 1)

        if issues:
            print(f"\n❌ Found {len(issues)} empty group(s)/repeat(s) (no enabled children):\n")
            for issue in issues:
                error_msg = (f"  Row {issue['row']}: group '{issue['name']}' "
                           f"(ends at row {issue['end_row']}) has no enabled children\n"
                           f"    SurveyCTO will reject this. Remove the group or ensure it has content.\n")
                print(error_msg)
                self.errors.append(error_msg)
        else:
            print("✅ All groups/repeats have enabled children")

        return len(issues) == 0

    def check_naming_conventions(self):
        """Check for naming convention issues in field names."""
        print("\n=== Checking Naming Conventions ===")

        issues = []

        for idx, row in self.survey_df.iterrows():
            field_name = str(row.get('name', ''))
            field_type = str(row.get('type', '')).lower()

            if not field_name or pd.isna(row.get('name')):
                continue

            # Skip metadata types
            if field_type in ['deviceid', 'subscriberid', 'simserial', 'phonenumber',
                             'username', 'start', 'end', 'caseid']:
                continue

            # Check for camelCase
            if re.search(r'[a-z][A-Z]', field_name):
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'issue': 'camelCase detected (use snake_case)'
                })

            # Check for dots in name (unusual, should use underscores)
            if '.' in field_name:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'issue': 'dot in name (use underscores for consistency)'
                })

            # Check for spaces
            if ' ' in field_name:
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'issue': 'space in name (use underscores)'
                })

            # Check for uppercase
            if field_name != field_name.lower():
                issues.append({
                    'row': idx + 2,
                    'field': field_name,
                    'issue': 'uppercase letters (use lowercase)'
                })

        if issues:
            print(f"\n⚠️  Found {len(issues)} naming convention issue(s):\n")
            for issue in issues:
                warning_msg = f"  Row {issue['row']}: '{issue['field']}' - {issue['issue']}\n"
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All field names follow conventions")

        return len(issues) == 0

    def check_conditional_formatting(self):
        """Check that conditional formatting rules are preserved in the survey sheet.

        The survey sheet uses type-based color coding to highlight different question types.
        These rules make the form easier to read and maintain. If they're removed (e.g., by
        re-saving with pandas), this check will fail.
        """
        print("\n=== Checking Conditional Formatting Rules ===")

        try:
            wb = openpyxl.load_workbook(self.file_path)
            ws = wb['survey']
        except Exception as e:
            print(f"⚠️  Could not load file with openpyxl: {e}")
            return True  # Don't fail if we can't check

        # Get all conditional formatting rules
        cf_rules = ws.conditional_formatting._cf_rules

        if not cf_rules:
            error_msg = ("❌ No conditional formatting rules found in survey sheet!\n"
                        "   The type-based color coding has been removed.\n"
                        "   Restore from a backup or re-apply formatting from template.")
            print(error_msg)
            self.errors.append(error_msg)
            wb.close()
            return False

        # Extract all formulas from the rules
        all_formulas = []
        for _, rules in cf_rules.items():
            for rule in rules:
                if rule.formula:
                    all_formulas.extend(rule.formula)

        # Expected formatting rules - these are the key type-based highlights
        expected_patterns = [
            ('begin group', '$A1="begin group"'),
            ('end group', '$A1="end group"'),
            ('begin repeat', '$A1="begin repeat"'),
            ('end repeat', '$A1="end repeat"'),
            ('text', '$A1="text"'),
            ('integer', '$A1="integer"'),
            ('decimal', '$A1="decimal"'),
            ('note', '$A1="note"'),
            ('calculate', 'calculate'),  # May be in OR with calculate_here
            ('select_one/select_multiple', 'select_'),  # Complex formula with LEFT()
            ('disabled rows', '$P1="yes"'),  # Strikethrough for disabled
            ('metadata fields', 'username'),  # Part of OR for metadata types
        ]

        missing_rules = []
        found_rules = []

        for rule_name, pattern in expected_patterns:
            found = any(pattern.lower() in formula.lower() for formula in all_formulas)
            if found:
                found_rules.append(rule_name)
            else:
                missing_rules.append(rule_name)

        # Count total rules
        total_rules = sum(len(rules) for rules in cf_rules.values())
        print(f"  Found {total_rules} conditional formatting rule(s)")
        print(f"  Type-based rules verified: {len(found_rules)}/{len(expected_patterns)}")

        if missing_rules:
            # Only error if many rules are missing (suggests major formatting loss)
            if len(missing_rules) > len(expected_patterns) // 2:
                error_msg = (f"❌ Many conditional formatting rules are missing!\n"
                            f"   Missing: {', '.join(missing_rules)}\n"
                            f"   The type-based color coding may have been damaged.\n"
                            f"   Consider restoring from backup.")
                print(error_msg)
                self.errors.append(error_msg)
                wb.close()
                return False
            else:
                warning_msg = (f"⚠️  Some conditional formatting rules may be missing:\n"
                              f"   {', '.join(missing_rules)}")
                print(warning_msg)
                self.warnings.append(warning_msg)
        else:
            print("✅ All expected conditional formatting rules are present")

        wb.close()
        return len(missing_rules) <= len(expected_patterns) // 2

    def check_formatting_preserved(self):
        """Check that cell formatting (especially red text for unverified translations) is preserved.

        Red text in Hindi columns indicates unverified translations that need review.
        If formatting is accidentally removed (e.g., by using pandas to save), this check will fail.
        """
        print("\n=== Checking Cell Formatting ===")

        # Reference file with known good formatting
        reference_file = self.file_path.parent / 'backups' / 'ai_health_pilot_baseline_backup_review_SAFE.xlsx'

        try:
            wb = openpyxl.load_workbook(self.file_path)
            ws = wb['survey']
        except Exception as e:
            print(f"⚠️  Could not load file with openpyxl: {e}")
            return True  # Don't fail if we can't check

        # Count formatted cells (non-black font colors)
        def count_formatted_cells(worksheet):
            formatted = 0
            red_text = 0
            for row in worksheet.iter_rows(min_row=1, max_row=min(worksheet.max_row, 300)):
                for cell in row:
                    if cell.font and cell.font.color:
                        color = cell.font.color
                        if color.type == 'rgb' and color.rgb:
                            rgb = color.rgb.upper()
                            if rgb not in ['00000000', 'FF000000', None, '']:
                                formatted += 1
                                # Check for red (FFFF0000 or 00FF0000)
                                if rgb in ['FFFF0000', '00FF0000'] or rgb.startswith('FFFF00'):
                                    red_text += 1
            return formatted, red_text

        current_formatted, current_red = count_formatted_cells(ws)
        wb.close()

        print(f"  Current file: {current_formatted} formatted cells, {current_red} red text cells")

        # Compare against reference if available
        if reference_file.exists():
            try:
                ref_wb = openpyxl.load_workbook(reference_file)
                ref_ws = ref_wb['survey']
                ref_formatted, ref_red = count_formatted_cells(ref_ws)
                ref_wb.close()

                print(f"  Reference file: {ref_formatted} formatted cells, {ref_red} red text cells")

                # Check if formatting has been significantly reduced
                if current_formatted < ref_formatted * 0.5:
                    error_msg = (f"  ❌ Formatting may have been removed!\n"
                                f"     Current: {current_formatted} formatted cells\n"
                                f"     Reference: {ref_formatted} formatted cells\n"
                                f"     Red text (unverified translations) may have been lost.\n"
                                f"     Restore from backup: {reference_file.name}")
                    print(error_msg)
                    self.errors.append(error_msg)
                    return False

                if current_red < ref_red * 0.5 and ref_red > 5:
                    warning_msg = (f"  ⚠️  Red text count reduced significantly\n"
                                  f"     Current: {current_red}, Reference: {ref_red}\n"
                                  f"     This may indicate translations were verified OR formatting was lost.")
                    print(warning_msg)
                    self.warnings.append(warning_msg)

            except Exception as e:
                print(f"  ⚠️  Could not load reference file: {e}")

        else:
            print(f"  ℹ️  No reference file found at {reference_file}")
            # Still check that there's SOME formatting if this is a Hindi survey
            if 'label:Hindi' in self.survey_df.columns and current_formatted == 0:
                warning_msg = ("  ⚠️  No formatted cells found in a multi-language survey.\n"
                             "     Red text should mark unverified Hindi translations.")
                print(warning_msg)
                self.warnings.append(warning_msg)

        print("✅ Formatting check complete")
        return True

    def check_version_formula(self):
        """Check that the version formula in settings has been evaluated.

        The settings sheet has a version formula that generates a timestamp (YYMMDDHHmm).
        If the formula hasn't been evaluated (no cached value), we run recalc_excel.sh
        to open Excel and force recalculation.
        """
        print("\n=== Checking Version Formula ===")

        try:
            # Load with data_only=True to get cached values
            wb_data = openpyxl.load_workbook(self.file_path, data_only=True)
            if 'settings' not in wb_data.sheetnames:
                print("  ℹ️  No settings sheet found")
                wb_data.close()
                return True

            settings = wb_data['settings']
            cached_value = settings['C2'].value
            wb_data.close()

            # Also check the formula itself
            wb_formula = openpyxl.load_workbook(self.file_path)
            formula = wb_formula['settings']['C2'].value
            wb_formula.close()

            # Check if it's a formula
            is_formula = formula and str(formula).startswith('=')

            if is_formula:
                print(f"  Formula: {str(formula)[:60]}...")

                if cached_value:
                    print(f"  ✅ Cached value: {cached_value}")
                    return True
                else:
                    print("  ⚠️  Version formula has not been evaluated (no cached value)")
                    print("  Attempting to recalculate via Excel...")

                    # Try to run recalc_excel.sh
                    recalc_script = Path(__file__).parent / 'recalc_excel.sh'
                    if recalc_script.exists():
                        try:
                            result = subprocess.run(
                                [str(recalc_script), str(self.file_path.absolute())],
                                capture_output=True,
                                text=True,
                                timeout=60
                            )
                            if result.returncode == 0:
                                print("  ✅ Recalculated formulas via Excel")

                                # Verify the cached value is now present
                                wb_verify = openpyxl.load_workbook(self.file_path, data_only=True)
                                new_cached = wb_verify['settings']['C2'].value
                                wb_verify.close()

                                if new_cached:
                                    print(f"  ✅ Version is now: {new_cached}")
                                    return True
                                else:
                                    # Excel may not have cached the value in a way openpyxl can read
                                    # But the formula is preserved, so Excel will calculate it when opened
                                    print("  ℹ️  Cached value not readable by openpyxl, but formula preserved")
                                    print("  ℹ️  Excel will calculate the version when the file is opened")
                                    return True
                            else:
                                print(f"  ⚠️  Excel recalculation failed: {result.stderr}")
                                self.warnings.append("Could not recalculate version formula")
                                return False
                        except subprocess.TimeoutExpired:
                            print("  ⚠️  Excel recalculation timed out")
                            self.warnings.append("Version formula recalculation timed out")
                            return False
                        except Exception as e:
                            print(f"  ⚠️  Could not run recalc script: {e}")
                            self.warnings.append(f"Could not recalculate version: {e}")
                            return False
                    else:
                        print(f"  ⚠️  recalc_excel.sh not found at {recalc_script}")
                        self.warnings.append("Version formula not evaluated, recalc script not found")
                        return False
            else:
                # It's a static value, not a formula
                if formula:
                    print(f"  Version (static): {formula}")
                else:
                    print("  ⚠️  No version set in settings")
                    self.warnings.append("No version set in settings sheet")
                return True

        except Exception as e:
            print(f"  ⚠️  Could not check version: {e}")
            return True  # Don't fail the whole check for this

    def run_all_checks(self):
        """Run all validation checks."""
        print(f"\n{'='*60}")
        print(f"SurveyCTO Form Checker")
        print(f"File: {self.file_path}")
        print(f"{'='*60}")

        if not self.load_form():
            return False

        results = []
        results.append(self.check_required_columns())
        results.append(self.check_blank_names())
        results.append(self.check_duplicate_names())
        results.append(self.check_empty_groups())
        results.append(self.check_expression_syntax())
        results.append(self.check_upload_parser_blockers())
        results.append(self.check_field_references())
        results.append(self.check_choices_field_references())
        results.append(self.check_choice_lists())
        results.append(self.check_other_specify_fields())
        results.append(self.check_select_multiple_other())
        results.append(self.check_select_multiple_exclusive())
        results.append(self.check_required_fields())
        results.append(self.check_typos())
        results.append(self.check_missing_constraint_messages())
        results.append(self.check_integer_constraints())
        results.append(self.check_numeric_refuse_option())
        results.append(self.check_calculate_fields())
        results.append(self.check_hindi_translations())
        results.append(self.check_naming_conventions())
        results.append(self.check_conditional_formatting())
        results.append(self.check_formatting_preserved())
        results.append(self.check_version_formula())

        # Print summary
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")

        if self.errors:
            print(f"\n❌ {len(self.errors)} ERROR(S) FOUND:")
            for i, error in enumerate(self.errors, 1):
                print(f"{i}. {error.strip()}")

        if self.warnings:
            print(f"\n⚠️  {len(self.warnings)} WARNING(S):")
            for i, warning in enumerate(self.warnings, 1):
                print(f"{i}. {warning.strip()}")

        if not self.errors and not self.warnings:
            print("\n✅ All checks passed! Form looks good.")
            return True
        elif not self.errors:
            print("\n✅ No errors found (but there are warnings to review)")
            return True
        else:
            print("\n❌ Form has errors that need to be fixed")
            return False


def main():
    """Main entry point."""
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    else:
        file_path = "ai_health_pilot_baseline.xlsx"
        print(f"No file specified, using default: {file_path}")

    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        sys.exit(1)

    checker = SurveyCTOChecker(file_path)
    success = checker.run_all_checks()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
