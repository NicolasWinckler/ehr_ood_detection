import seaborn as sns
import matplotlib.pyplot as plt
import pandas as pd


# TODO: Is this still up to date?
def barplot_from_nested_dict(
    nested_dict,
    metric_name="OOD detection AUC",
    group_name="OOD group",
    vline=None,
    xlim=(0, 1.0),
    save_dir=None,
    height=6,
    aspect=1.5,
    legend_out=False,
):
    sns.set_style("whitegrid")
    sns.set_palette("Set1", 10)
    df = pd.DataFrame.from_dict(nested_dict)

    df = df.stack().reset_index()
    df.columns = [group_name, "", metric_name]

    sns.catplot(
        x=metric_name,
        y=group_name,
        hue="",
        data=df,
        kind="bar",
        height=height,
        aspect=aspect,
        facet_kws=dict(despine=False),
        alpha=0.9,
        legend_out=legend_out,
    )
    plt.xlim(xlim)
    if vline:
        plt.axvline(vline, linestyle="--")
    if save_dir:
        plt.savefig(save_dir, dpi=300, bbox_inches="tight", pad=0)
        plt.close()
    else:
        plt.show()


def boxplot_from_nested_listdict(
    nested_dict,
    name,
    hline=None,
    vline=None,
    xlim=(0.0, 1.0),
    ylim=(0.0, 1.0),
    dummy_group_name=None,
    x_name="scale",
    horizontal=False,
    legend_out=False,
    figsize=None,
    save_dir=None,
    kind="box",
    **kwargs
):
    sns.set_palette("Set1", 10)
    sns.set_style("whitegrid")
    df = pd.DataFrame.from_dict(nested_dict, orient="columns")

    df = df.stack().reset_index()
    df.columns = [x_name, "", name]
    df = df.explode(name)
    if dummy_group_name:
        methods = [m for m in df[""].unique()]
        fake_df = pd.DataFrame(
            {
                x_name: [dummy_group_name] * len(methods),
                "": methods,
                name: [0] * len(methods),
            }
        )
        df = pd.concat([fake_df, df])
    plt.figure(figsize=figsize)
    if horizontal:
        sns.catplot(
            x=name,
            y=x_name,
            hue="",
            data=df,
            kind=kind,
            legend_out=legend_out,
            facet_kws=dict(despine=False),
            **kwargs
        )

    else:
        sns.catplot(
            x=x_name,
            y=name,
            hue="",
            data=df,
            kind=kind,
            legend_out=legend_out,
            facet_kws=dict(despine=False),
            **kwargs
        )

    plt.ylim(ylim)
    plt.xlim(xlim)
    if hline:
        plt.axhline(hline, linestyle="--")
    if vline:
        plt.axvline(vline, linestyle="--")
    if save_dir:
        plt.savefig(save_dir, dpi=300, bbox_inches="tight", pad=0)
        plt.close()
    else:
        plt.show()
