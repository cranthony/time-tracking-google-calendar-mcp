//! `#[derive(Schema)]` and `#[tool]`: a tool's argument and result types,
//! and the tool itself, described as the JSON schemas tools/list sends.
//! The schemas are built at run time by `ctt::contract` (see there for
//! their shape); these only gather what the code says -- each type's
//! fields, doc comments and `#[schema(...)]` options.

use proc_macro::TokenStream;
use proc_macro2::TokenStream as Tokens;
use quote::{format_ident, quote};
use syn::{
    parse_macro_input, Attribute, Data, DeriveInput, Expr, Fields, FnArg, GenericArgument, ItemFn, Lit, LitStr, Meta, Pat,
    PathArguments, ReturnType, Type,
};

/// A type's schema: a struct with named fields is a model (in `$defs`,
/// referred to by `$ref`); an enum of unit variants is a string literal's
/// `enum` (one variant: its `const`).
///
/// On the type: `#[schema(title = "...")]` (default: its name),
/// `#[schema(deny_unknown)]` (`additionalProperties: false`). On a field:
/// `#[schema(default = "<json>")]`, and `#[schema(required)]` for an
/// `Option` that has no default (otherwise its default is null). Doc
/// comments are descriptions.
#[proc_macro_derive(Schema, attributes(schema))]
pub fn derive_schema(input: TokenStream) -> TokenStream {
    let input = parse_macro_input!(input as DeriveInput);
    match schema_impl(&input) {
        Ok(tokens) => tokens.into(),
        Err(error) => error.to_compile_error().into(),
    }
}

/// A tool: the function's doc comment is its description, its parameters
/// after the first (`&App`) its arguments, and the `T` of its
/// `Result<T, ToolError>` its result. `#[tool(writes)]` for a tool that
/// may write (it holds WRITE_LOCK). A parameter takes the same options as
/// a field, plus `description = "..."`. Adds `<name>_tool()`, the
/// `ToolDef` the registry lists.
#[proc_macro_attribute]
pub fn tool(attr: TokenStream, item: TokenStream) -> TokenStream {
    let writes = !attr.is_empty() && attr.to_string().trim() == "writes";
    if !attr.is_empty() && !writes {
        return syn::Error::new(proc_macro2::Span::call_site(), "expected #[tool] or #[tool(writes)]").to_compile_error().into();
    }
    let function = parse_macro_input!(item as ItemFn);
    match tool_impl(function, writes) {
        Ok(tokens) => tokens.into(),
        Err(error) => error.to_compile_error().into(),
    }
}

/// The doc comment's text: each line without the one space after `///`,
/// joined by newlines.
fn doc(attrs: &[Attribute]) -> Option<String> {
    let lines: Vec<String> = attrs
        .iter()
        .filter(|a| a.path().is_ident("doc"))
        .filter_map(|a| match &a.meta {
            Meta::NameValue(nv) => match &nv.value {
                Expr::Lit(lit) => match &lit.lit {
                    Lit::Str(s) => Some(s.value()),
                    _ => None,
                },
                _ => None,
            },
            _ => None,
        })
        .map(|line| line.strip_prefix(' ').map(str::to_string).unwrap_or(line))
        .collect();
    if lines.is_empty() {
        None
    } else {
        Some(lines.join("\n"))
    }
}

#[derive(Default)]
struct Options {
    title: Option<String>,
    deny_unknown: bool,
    default: Option<String>,
    required: bool,
    description: Option<String>,
}

fn options(attrs: &[Attribute]) -> syn::Result<Options> {
    let mut options = Options::default();
    for attr in attrs.iter().filter(|a| a.path().is_ident("schema")) {
        attr.parse_nested_meta(|meta| {
            if meta.path.is_ident("deny_unknown") {
                options.deny_unknown = true;
            } else if meta.path.is_ident("required") {
                options.required = true;
            } else if meta.path.is_ident("title") {
                options.title = Some(meta.value()?.parse::<LitStr>()?.value());
            } else if meta.path.is_ident("default") {
                options.default = Some(meta.value()?.parse::<LitStr>()?.value());
            } else if meta.path.is_ident("description") {
                options.description = Some(meta.value()?.parse::<LitStr>()?.value());
            } else {
                return Err(meta.error("unknown schema option"));
            }
            Ok(())
        })?;
    }
    Ok(options)
}

/// The name serde gives a field or parameter: without `r#`.
fn wire_name(ident: &syn::Ident) -> String {
    let name = ident.to_string();
    name.strip_prefix("r#").map(str::to_string).unwrap_or(name)
}

fn opt_str(value: &Option<String>) -> Tokens {
    match value {
        Some(text) => quote!(::core::option::Option::Some(#text)),
        None => quote!(::core::option::Option::None),
    }
}

/// `contract::Field`s for named fields or parameters.
fn field_tokens(name: &str, ty: &Type, opts: &Options, doc_text: Option<String>) -> Tokens {
    let default = opt_str(&opts.default);
    let description = opt_str(&opts.description.clone().or(doc_text));
    let required = opts.required;
    quote! {
        crate::contract::Field::new::<#ty>(defs, #name, #default, #required, #description)
    }
}

fn schema_impl(input: &DeriveInput) -> syn::Result<Tokens> {
    let ident = &input.ident;
    let opts = options(&input.attrs)?;
    let title = opts.title.clone().unwrap_or_else(|| ident.to_string());
    let description = opt_str(&doc(&input.attrs));
    let body = match &input.data {
        Data::Struct(data) => {
            let Fields::Named(named) = &data.fields else {
                return Err(syn::Error::new_spanned(ident, "Schema needs named fields"));
            };
            let mut fields = Vec::new();
            for field in &named.named {
                let field_opts = options(&field.attrs)?;
                fields.push(field_tokens(&wire_name(field.ident.as_ref().unwrap()), &field.ty, &field_opts, doc(&field.attrs)));
            }
            let deny = opts.deny_unknown;
            quote! {
                if let ::core::option::Option::Some(reference) = defs.reference(#title) {
                    return reference;
                }
                let fields = ::std::vec![#(#fields),*];
                crate::contract::model(defs, #title, #description, #deny, fields)
            }
        }
        Data::Enum(data) => {
            let rename_all = serde_rename_all(&input.attrs)?;
            let mut names = Vec::new();
            for variant in &data.variants {
                if !matches!(variant.fields, Fields::Unit) {
                    return Err(syn::Error::new_spanned(variant, "Schema enums are unit variants"));
                }
                let name = match serde_rename(&variant.attrs)? {
                    Some(name) => name,
                    None => apply_rename_all(&variant.ident.to_string(), rename_all.as_deref()),
                };
                names.push(name);
            }
            quote! {
                crate::contract::literal(&[#(#names),*])
            }
        }
        Data::Union(_) => return Err(syn::Error::new_spanned(ident, "Schema doesn't do unions")),
    };
    Ok(quote! {
        impl crate::contract::Schema for #ident {
            fn schema(defs: &mut crate::contract::Defs) -> ::serde_json::Value {
                #body
            }
        }
    })
}

fn serde_rename_all(attrs: &[Attribute]) -> syn::Result<Option<String>> {
    let mut found = None;
    for attr in attrs.iter().filter(|a| a.path().is_ident("serde")) {
        attr.parse_nested_meta(|meta| {
            if meta.path.is_ident("rename_all") {
                found = Some(meta.value()?.parse::<LitStr>()?.value());
            } else if meta.input.peek(syn::Token![=]) {
                meta.value()?.parse::<Expr>()?;
            }
            Ok(())
        })?;
    }
    Ok(found)
}

fn serde_rename(attrs: &[Attribute]) -> syn::Result<Option<String>> {
    let mut found = None;
    for attr in attrs.iter().filter(|a| a.path().is_ident("serde")) {
        attr.parse_nested_meta(|meta| {
            if meta.path.is_ident("rename") {
                found = Some(meta.value()?.parse::<LitStr>()?.value());
            } else if meta.input.peek(syn::Token![=]) {
                meta.value()?.parse::<Expr>()?;
            }
            Ok(())
        })?;
    }
    Ok(found)
}

fn apply_rename_all(variant: &str, rule: Option<&str>) -> String {
    match rule {
        Some("lowercase") => variant.to_lowercase(),
        Some("snake_case") => {
            let mut out = String::new();
            for (i, c) in variant.chars().enumerate() {
                if c.is_uppercase() {
                    if i > 0 {
                        out.push('_');
                    }
                    out.extend(c.to_lowercase());
                } else {
                    out.push(c);
                }
            }
            out
        }
        _ => variant.to_string(),
    }
}

/// The `T` of `Result<T, _>` (or of any one-or-two-argument generic, such
/// as a `ToolOutcome<T>` alias).
fn result_type(output: &ReturnType) -> syn::Result<Type> {
    let ReturnType::Type(_, ty) = output else {
        return Err(syn::Error::new_spanned(output, "a tool returns Result<T, ToolError>"));
    };
    if let Type::Path(path) = ty.as_ref() {
        if let Some(last) = path.path.segments.last() {
            if let PathArguments::AngleBracketed(args) = &last.arguments {
                if let Some(GenericArgument::Type(t)) = args.args.first() {
                    return Ok(t.clone());
                }
            }
        }
    }
    Err(syn::Error::new_spanned(ty, "a tool returns Result<T, ToolError>"))
}

fn tool_impl(mut function: ItemFn, writes: bool) -> syn::Result<Tokens> {
    let name = function.sig.ident.clone();
    let name_text = name.to_string();
    let description = doc(&function.attrs).unwrap_or_default();
    let output = result_type(&function.sig.output)?;
    let args_struct = format_ident!("__{}_arguments", name);
    let def_fn = format_ident!("{}_tool", name);

    let mut field_defs = Vec::new();
    let mut struct_fields = Vec::new();
    let mut call_args = Vec::new();
    for (i, input) in function.sig.inputs.iter_mut().enumerate() {
        let FnArg::Typed(typed) = input else {
            return Err(syn::Error::new_spanned(input, "a tool is a free function"));
        };
        let opts = options(&typed.attrs)?;
        typed.attrs.retain(|a| !a.path().is_ident("schema"));
        if i == 0 {
            continue;
        }
        let Pat::Ident(pat) = typed.pat.as_ref() else {
            return Err(syn::Error::new_spanned(&typed.pat, "a tool's parameters are plain names"));
        };
        let ident = &pat.ident;
        let ty = &typed.ty;
        field_defs.push(field_tokens(&wire_name(ident), ty, &opts, None));
        struct_fields.push(quote! {
            #[serde(default)]
            #ident: ::core::option::Option<#ty>
        });
        call_args.push(quote! {
            arguments.#ident.unwrap_or_else(|| crate::contract::absent::<#ty>())
        });
    }
    let input_title = format!("{name_text}Arguments");
    Ok(quote! {
        #function

        #[allow(non_camel_case_types)]
        #[derive(::serde::Deserialize)]
        struct #args_struct {
            #(#struct_fields),*
        }

        /// This tool, for the registry (see `#[tool]`).
        pub fn #def_fn() -> crate::contract::ToolDef {
            fn input(defs: &mut crate::contract::Defs) -> ::serde_json::Value {
                let fields = ::std::vec![#(#field_defs),*];
                crate::contract::arguments(defs, #input_title, fields)
            }
            fn output(defs: &mut crate::contract::Defs) -> ::serde_json::Value {
                <#output as crate::contract::Schema>::schema(defs)
            }
            fn run(app: &crate::app::App, arguments: ::serde_json::Value) -> ::core::result::Result<::serde_json::Value, crate::tools::ToolError> {
                let arguments: #args_struct = crate::contract::from_arguments(arguments)?;
                let result = #name(app, #(#call_args),*)?;
                crate::contract::to_result(&result)
            }
            crate::contract::ToolDef { name: #name_text, description: #description, writes: #writes, input, output, run }
        }
    })
}
